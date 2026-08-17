/* Минимальный markdown-рендерер без зависимостей. */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function inline(s) {
    s = esc(s);
    // код
    s = s.replace(/`([^`\n]+)`/g, function (_, c) { return '<code>' + c + '</code>'; });
    // картинки
    s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
      '<img class="img-out" src="$2" alt="$1" loading="lazy">');
    // ссылки
    s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // голые url
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    s = s.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    return s;
  }

  function render(src) {
    if (!src) return '';
    var lines = String(src).replace(/\r\n/g, '\n').split('\n');
    var out = [], i = 0;

    function flushList(tag, items) {
      out.push('<' + tag + '>' + items.map(function (x) {
        return '<li>' + inline(x) + '</li>';
      }).join('') + '</' + tag + '>');
    }

    while (i < lines.length) {
      var ln = lines[i];

      // блок кода
      var fence = ln.match(/^\s*```+\s*([\w+-]*)\s*$/);
      if (fence) {
        var lang = fence[1] || '';
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        out.push('<pre data-lang="' + esc(lang) + '"><code>' + esc(buf.join('\n')) + '</code></pre>');
        continue;
      }

      // заголовки
      var h = ln.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        var lv = Math.min(h[1].length, 3);
        out.push('<h' + lv + '>' + inline(h[2]) + '</h' + lv + '>');
        i++; continue;
      }

      // разделитель
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(ln)) { out.push('<hr>'); i++; continue; }

      // таблица
      if (/\|/.test(ln) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
        var head = ln.split('|').map(function (c) { return c.trim(); });
        if (head[0] === '') head.shift();
        if (head[head.length - 1] === '') head.pop();
        i += 2;
        var rows = [];
        while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) {
          var cells = lines[i].split('|').map(function (c) { return c.trim(); });
          if (cells[0] === '') cells.shift();
          if (cells[cells.length - 1] === '') cells.pop();
          rows.push(cells); i++;
        }
        out.push('<table><thead><tr>' + head.map(function (c) {
          return '<th>' + inline(c) + '</th>';
        }).join('') + '</tr></thead><tbody>' + rows.map(function (r) {
          return '<tr>' + r.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
        }).join('') + '</tbody></table>');
        continue;
      }

      // цитата
      if (/^\s*>\s?/.test(ln)) {
        var q = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          q.push(lines[i].replace(/^\s*>\s?/, '')); i++;
        }
        out.push('<blockquote>' + render(q.join('\n')) + '</blockquote>');
        continue;
      }

      // маркированный список
      if (/^\s*[-*+]\s+/.test(ln)) {
        var ul = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          ul.push(lines[i].replace(/^\s*[-*+]\s+/, '')); i++;
        }
        flushList('ul', ul); continue;
      }

      // нумерованный список
      if (/^\s*\d+[.)]\s+/.test(ln)) {
        var ol = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          ol.push(lines[i].replace(/^\s*\d+[.)]\s+/, '')); i++;
        }
        flushList('ol', ol); continue;
      }

      // пустая строка
      if (!ln.trim()) { i++; continue; }

      // абзац
      var p = [];
      while (i < lines.length && lines[i].trim() &&
             !/^\s*(#{1,6}\s|>|[-*+]\s|\d+[.)]\s|```)/.test(lines[i])) {
        p.push(lines[i]); i++;
      }
      out.push('<p>' + inline(p.join('\n')).replace(/\n/g, '<br>') + '</p>');
    }
    return out.join('');
  }

  global.MD = { render: render, esc: esc, inline: inline };
})(window);
