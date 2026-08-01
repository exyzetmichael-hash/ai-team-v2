(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useMemo, useRef } = SDK.hooks;
  const {
    Card, CardHeader, CardTitle, CardContent,
    Badge, Input, Separator, Tabs, TabsList, TabsTrigger,
  } = SDK.components;
  const h = React.createElement;

  // ---------------------------------------------------------------------
  // Markdown → HTML (минимальный самописный рендерер, без зависимостей —
  // плагины дашборда Hermes не бандлят сторонние либы, см. extending-the-
  // dashboard.md). Покрывает то, что реально встречается в vault EDITH:
  // заголовки, жирный/курсив, инлайн-код, код-блоки, списки, цитаты,
  // ссылки и [[wikilink]]. Не полный CommonMark — этого достаточно для
  // просмотра, редактирует заметки EDITH, не этот вьюер.
  // ---------------------------------------------------------------------

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderInline(text, stemToPath) {
    let out = escapeHtml(text);

    // [[Note]] / [[Note|Alias]] / [[Note#Heading]] — до обычных ссылок и
    // курсива, чтобы не конфликтовать с их разметкой.
    out = out.replace(/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]/g, (m, name, alias) => {
      const cleanName = name.trim();
      const stem = cleanName.split("/").pop().toLowerCase();
      const path = stemToPath.get(stem);
      const label = escapeHtml((alias || cleanName).trim());
      if (path) {
        return `<a href="#" class="vault-wikilink" data-note-path="${escapeHtml(path)}">${label}</a>`;
      }
      return `<span class="vault-wikilink-missing" title="Заметка не найдена">${label}</span>`;
    });

    // [text](url)
    out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, label, url) => {
      const safeUrl = /^(https?:|mailto:|#)/i.test(url) ? url : "#";
      return `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
    });

    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, "<em>$1</em>");
    return out;
  }

  function renderMarkdown(md, stemToPath) {
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let inCodeBlock = false;
    let codeLines = [];
    let listBuffer = [];
    let listType = null;

    function flushList() {
      if (listBuffer.length) {
        const tag = listType === "ol" ? "ol" : "ul";
        html.push(`<${tag}>${listBuffer.map((li) => `<li>${renderInline(li, stemToPath)}</li>`).join("")}</${tag}>`);
        listBuffer = [];
        listType = null;
      }
    }

    for (const rawLine of lines) {
      const line = rawLine;

      if (line.trim().startsWith("```")) {
        if (inCodeBlock) {
          html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
          codeLines = [];
          inCodeBlock = false;
        } else {
          flushList();
          inCodeBlock = true;
        }
        continue;
      }
      if (inCodeBlock) {
        codeLines.push(line);
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushList();
        const level = heading[1].length;
        html.push(`<h${level}>${renderInline(heading[2], stemToPath)}</h${level}>`);
        continue;
      }

      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushList();
        html.push(`<blockquote>${renderInline(quote[1], stemToPath)}</blockquote>`);
        continue;
      }

      const ol = line.match(/^\s*\d+\.\s+(.*)$/);
      if (ol) {
        if (listType && listType !== "ol") flushList();
        listType = "ol";
        listBuffer.push(ol[1]);
        continue;
      }

      const ul = line.match(/^\s*[-*]\s+(.*)$/);
      if (ul) {
        if (listType && listType !== "ul") flushList();
        listType = "ul";
        listBuffer.push(ul[1]);
        continue;
      }

      flushList();

      if (line.trim() === "") {
        continue;
      }

      html.push(`<p>${renderInline(line, stemToPath)}</p>`);
    }
    flushList();
    if (inCodeBlock && codeLines.length) {
      html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
    }
    return html.join("\n");
  }

  // ---------------------------------------------------------------------
  // Дерево папок из плоского списка путей
  // ---------------------------------------------------------------------

  function buildTree(paths) {
    const root = { folders: new Map(), files: [] };
    for (const p of paths) {
      const parts = p.split("/");
      let node = root;
      for (let i = 0; i < parts.length - 1; i++) {
        const seg = parts[i];
        if (!node.folders.has(seg)) {
          node.folders.set(seg, { folders: new Map(), files: [] });
        }
        node = node.folders.get(seg);
      }
      node.files.push({ name: parts[parts.length - 1], path: p });
    }
    return root;
  }

  function FolderNode({ name, node, depth, onSelect, selected, openSet, toggleOpen }) {
    const key = name || "__root__";
    const isOpen = depth === 0 || openSet.has(key + depth);
    const folders = Array.from(node.folders.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    const files = node.files.slice().sort((a, b) => a.name.localeCompare(b.name));

    return h(
      "div",
      { style: { marginLeft: depth === 0 ? 0 : 10 } },
      name
        ? h(
            "div",
            {
              onClick: () => toggleOpen(key + depth),
              style: {
                cursor: "pointer",
                fontSize: "0.8rem",
                fontWeight: 600,
                padding: "2px 0",
                color: "var(--color-muted-foreground)",
              },
            },
            (isOpen ? "▾ " : "▸ ") + name,
          )
        : null,
      isOpen
        ? h(
            "div",
            null,
            folders.map(([fname, fnode]) =>
              h(FolderNode, {
                key: fname,
                name: fname,
                node: fnode,
                depth: depth + 1,
                onSelect,
                selected,
                openSet,
                toggleOpen,
              }),
            ),
            files.map((f) =>
              h(
                "div",
                {
                  key: f.path,
                  onClick: () => onSelect(f.path),
                  style: {
                    cursor: "pointer",
                    fontSize: "0.85rem",
                    padding: "3px 4px",
                    marginLeft: depth === 0 ? 0 : 4,
                    borderRadius: 4,
                    background: selected === f.path ? "var(--color-accent)" : "transparent",
                    color: selected === f.path ? "var(--color-accent-foreground)" : "var(--color-foreground)",
                  },
                },
                "📄 " + f.name.replace(/\.md$/, ""),
              ),
            ),
          )
        : null,
    );
  }

  // ---------------------------------------------------------------------
  // Граф связей — force-directed layout (Fruchterman-Reingold), без
  // внешних библиотек, рассчитывается один раз при загрузке графа.
  // ---------------------------------------------------------------------

  function computeLayout(nodes, edges, width, height) {
    const positions = {};
    nodes.forEach((n, i) => {
      const angle = (i / Math.max(nodes.length, 1)) * Math.PI * 2;
      const r = Math.min(width, height) / 3;
      positions[n.id] = {
        x: width / 2 + Math.cos(angle) * r,
        y: height / 2 + Math.sin(angle) * r,
      };
    });
    if (nodes.length < 2) return positions;

    const area = width * height;
    const k = Math.sqrt(area / nodes.length);
    const iterations = 250;

    for (let iter = 0; iter < iterations; iter++) {
      const disp = {};
      nodes.forEach((n) => (disp[n.id] = { x: 0, y: 0 }));

      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i].id;
          const b = nodes[j].id;
          let dx = positions[a].x - positions[b].x;
          let dy = positions[a].y - positions[b].y;
          let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
          const force = (k * k) / dist;
          dx /= dist;
          dy /= dist;
          disp[a].x += dx * force;
          disp[a].y += dy * force;
          disp[b].x -= dx * force;
          disp[b].y -= dy * force;
        }
      }

      edges.forEach((e) => {
        if (!positions[e.source] || !positions[e.target]) return;
        let dx = positions[e.source].x - positions[e.target].x;
        let dy = positions[e.source].y - positions[e.target].y;
        let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const force = (dist * dist) / k;
        dx /= dist;
        dy /= dist;
        disp[e.source].x -= dx * force;
        disp[e.source].y -= dy * force;
        disp[e.target].x += dx * force;
        disp[e.target].y += dy * force;
      });

      const temp = Math.max(width, height) * 0.1 * (1 - iter / iterations);
      nodes.forEach((n) => {
        const d = disp[n.id];
        const dist = Math.sqrt(d.x * d.x + d.y * d.y) || 0.01;
        const clamped = Math.min(dist, Math.max(temp, 0.5));
        positions[n.id].x += (d.x / dist) * clamped;
        positions[n.id].y += (d.y / dist) * clamped;
        positions[n.id].x = Math.min(width - 24, Math.max(24, positions[n.id].x));
        positions[n.id].y = Math.min(height - 24, Math.max(24, positions[n.id].y));
      });
    }
    return positions;
  }

  function GraphView({ graph, onSelectNote }) {
    const width = 900;
    const height = 620;
    const positions = useMemo(() => {
      if (!graph) return {};
      return computeLayout(graph.nodes, graph.edges, width, height);
    }, [graph]);

    if (!graph) {
      return h("p", { className: "text-sm text-muted-foreground" }, "Загрузка графа…");
    }
    if (graph.nodes.length === 0) {
      return h("p", { className: "text-sm text-muted-foreground" }, "В vault нет заметок.");
    }

    const degree = {};
    graph.edges.forEach((e) => {
      degree[e.source] = (degree[e.source] || 0) + 1;
      degree[e.target] = (degree[e.target] || 0) + 1;
    });

    return h(
      "svg",
      { width: "100%", viewBox: `0 0 ${width} ${height}`, style: { background: "var(--color-card)", borderRadius: "var(--radius)" } },
      h(
        "g",
        null,
        graph.edges.map((e, i) =>
          positions[e.source] && positions[e.target]
            ? h("line", {
                key: "e" + i,
                x1: positions[e.source].x,
                y1: positions[e.source].y,
                x2: positions[e.target].x,
                y2: positions[e.target].y,
                stroke: "var(--color-border)",
                strokeWidth: 1,
              })
            : null,
        ),
      ),
      h(
        "g",
        null,
        graph.nodes.map((n) => {
          const pos = positions[n.id];
          if (!pos) return null;
          const r = 4 + Math.min(10, (degree[n.id] || 0) * 1.5);
          return h(
            "g",
            {
              key: n.id,
              transform: `translate(${pos.x}, ${pos.y})`,
              style: { cursor: "pointer" },
              onClick: () => onSelectNote(n.id),
            },
            h("circle", { r, fill: "var(--color-primary)", opacity: 0.85 }),
            h(
              "text",
              {
                x: r + 4,
                y: 4,
                fontSize: 10,
                fill: "var(--color-foreground)",
              },
              n.label,
            ),
          );
        }),
      ),
    );
  }

  // ---------------------------------------------------------------------
  // Главный компонент
  // ---------------------------------------------------------------------

  function VaultPage() {
    const [view, setView] = useState("notes");
    const [tree, setTree] = useState(null);
    const [error, setError] = useState(null);
    const [selected, setSelected] = useState(null);
    const [content, setContent] = useState("");
    const [contentLoading, setContentLoading] = useState(false);
    const [graph, setGraph] = useState(null);
    const [filter, setFilter] = useState("");
    const [openSet] = useState(() => new Set());
    const [, forceRender] = useState(0);

    useEffect(() => {
      SDK.fetchJSON("/api/plugins/edith-vault/tree")
        .then((data) => setTree(data.notes || []))
        .catch((err) => setError(String(err)));
    }, []);

    useEffect(() => {
      if (view === "graph" && !graph) {
        SDK.fetchJSON("/api/plugins/edith-vault/graph")
          .then((data) => setGraph(data))
          .catch((err) => setError(String(err)));
      }
    }, [view]);

    function selectNote(path) {
      setSelected(path);
      setContentLoading(true);
      SDK.fetchJSON("/api/plugins/edith-vault/note?path=" + encodeURIComponent(path))
        .then((data) => setContent(data.content || ""))
        .catch((err) => setContent("Ошибка загрузки: " + err))
        .finally(() => setContentLoading(false));
    }

    function selectNoteFromGraph(path) {
      setView("notes");
      selectNote(path);
    }

    const stemToPath = useMemo(() => {
      const m = new Map();
      (tree || []).forEach((p) => {
        const stem = p.split("/").pop().replace(/\.md$/, "").toLowerCase();
        if (!m.has(stem)) m.set(stem, p);
      });
      return m;
    }, [tree]);

    const contentHtml = useMemo(() => {
      if (!content) return "";
      return renderMarkdown(content, stemToPath);
    }, [content, stemToPath]);

    function onContentClick(e) {
      const link = e.target.closest && e.target.closest(".vault-wikilink");
      if (link) {
        e.preventDefault();
        const path = link.getAttribute("data-note-path");
        if (path) selectNote(path);
      }
    }

    function toggleOpen(key) {
      if (openSet.has(key)) openSet.delete(key);
      else openSet.add(key);
      forceRender((n) => n + 1);
    }

    const filteredTree = useMemo(() => {
      const paths = tree || [];
      const f = filter.trim().toLowerCase();
      const filtered = f ? paths.filter((p) => p.toLowerCase().includes(f)) : paths;
      return buildTree(filtered);
    }, [tree, filter]);

    if (error) {
      return h(
        Card,
        null,
        h(CardHeader, null, h(CardTitle, null, "Vault")),
        h(CardContent, null, h("p", { className: "text-sm text-destructive" }, error)),
      );
    }

    return h(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "1rem" } },
      h(
        Tabs,
        { value: view, onValueChange: setView },
        h(
          TabsList,
          null,
          h(TabsTrigger, { value: "notes" }, "Заметки"),
          h(TabsTrigger, { value: "graph" }, "Граф"),
        ),
      ),
      view === "notes"
        ? h(
            "div",
            { style: { display: "flex", gap: "1rem", alignItems: "flex-start" } },
            h(
              Card,
              { style: { width: 280, flexShrink: 0, maxHeight: "75vh", overflow: "auto" } },
              h(
                CardHeader,
                null,
                h(CardTitle, { style: { fontSize: "0.95rem" } }, `Заметки (${(tree || []).length})`),
              ),
              h(
                CardContent,
                null,
                h(Input, {
                  placeholder: "Поиск…",
                  value: filter,
                  onChange: (e) => setFilter(e.target.value),
                  style: { marginBottom: "0.5rem" },
                }),
                h(Separator, { style: { marginBottom: "0.5rem" } }),
                tree === null
                  ? h("p", { className: "text-sm text-muted-foreground" }, "Загрузка…")
                  : h(FolderNode, {
                      name: "",
                      node: filteredTree,
                      depth: 0,
                      onSelect: selectNote,
                      selected,
                      openSet,
                      toggleOpen,
                    }),
              ),
            ),
            h(
              Card,
              { style: { flex: 1, minHeight: "75vh" } },
              h(
                CardHeader,
                null,
                h(CardTitle, { style: { fontSize: "0.95rem" } }, selected || "Выбери заметку слева"),
              ),
              h(
                CardContent,
                null,
                !selected
                  ? h("p", { className: "text-sm text-muted-foreground" }, "Заметка не выбрана.")
                  : contentLoading
                  ? h("p", { className: "text-sm text-muted-foreground" }, "Загрузка…")
                  : h("div", {
                      className: "vault-note-content",
                      onClick: onContentClick,
                      dangerouslySetInnerHTML: { __html: contentHtml },
                    }),
              ),
            ),
          )
        : h(
            Card,
            null,
            h(CardHeader, null, h(CardTitle, { style: { fontSize: "0.95rem" } }, "Граф связей")),
            h(CardContent, null, h(GraphView, { graph, onSelectNote: selectNoteFromGraph })),
          ),
    );
  }

  window.__HERMES_PLUGINS__.register("edith-vault", VaultPage);
})();
