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

  /* BG: МАТЕМАТИКА — настоящий мини-LaTeX. Модель пишет \( ... \) и
     \[ ... \]; мы превращаем это в красивый HTML: дроби (стопка с
     чертой), корни (знак + черта сверху), степени/индексы, греческие
     буквы, операторы, вектора, \text, \mathbb, функции прямым начертанием.
     Неизвестные команды не ломают вывод: остаётся читаемый текст. */
  var GREEK = { alpha:'\u03b1', beta:'\u03b2', gamma:'\u03b3', delta:'\u03b4',
    epsilon:'\u03b5', zeta:'\u03b6', eta:'\u03b7', theta:'\u03b8', iota:'\u03b9',
    kappa:'\u03ba', lambda:'\u03bb', mu:'\u03bc', nu:'\u03bd', xi:'\u03be',
    pi:'\u03c0', rho:'\u03c1', sigma:'\u03c3', tau:'\u03c4', upsilon:'\u03c5',
    phi:'\u03c6', chi:'\u03c7', psi:'\u03c8', omega:'\u03c9',
    Gamma:'\u0393', Delta:'\u0394', Theta:'\u0398', Lambda:'\u039b', Xi:'\u039e',
    Pi:'\u03a0', Sigma:'\u03a3', Phi:'\u03a6', Psi:'\u03a8', Omega:'\u03a9' };
  var SYM = { triangle:'\u25b3', angle:'\u2220', perp:'\u22a5', parallel:'\u2225',
    cdot:'\u00b7', times:'\u00d7', div:'\u00f7', pm:'\u00b1', mp:'\u2213',
    leq:'\u2264', geq:'\u2265', neq:'\u2260', ne:'\u2260', approx:'\u2248',
    sim:'\u223c', equiv:'\u2261', propto:'\u221d', to:'\u2192',
    rightarrow:'\u2192', leftarrow:'\u2190', Rightarrow:'\u21d2',
    leftrightarrow:'\u2194', Leftrightarrow:'\u21d4', infty:'\u221e',
    int:'\u222b', iint:'\u222c', iiint:'\u222d', oint:'\u222e',
    sum:'\u2211', prod:'\u220f', lim:'lim', partial:'\u2202', nabla:'\u2207',
    forall:'\u2200', exists:'\u2203', in:'\u2208', notin:'\u2209',
    subset:'\u2282', subseteq:'\u2286', cup:'\u222a', cap:'\u2229',
    emptyset:'\u2205', varnothing:'\u2205', cdots:'\u22ef', ldots:'\u2026',
    dots:'\u2026', vdots:'\u22ee', prime:'\u2032', circ:'\u2218',
    degree:'\u00b0', ell:'\u2113', hbar:'\u210f', anglebr:'\u27e8' };
  var FUNCS = ['arcsin','arccos','arctan','sinh','cosh','tanh','sin','cos','tan',
    'log','ln','lg','exp','det','dim','deg','arg','min','max','gcd','sec','csc','cot'];
  var BB = { R:'\u211d', N:'\u2115', Z:'\u2124', Q:'\u211a', C:'\u2102' };

  function mesc(t) { return esc(t); }

  /* разбор группы {..} или одного символа после префикса */
  function groupAt(src, i) {
    if (src[i] === '{') {
      var depth = 0, j = i;
      for (; j < src.length; j++) {
        if (src[j] === '{') depth++;
        else if (src[j] === '}') { depth--; if (!depth) break; }
      }
      return { text: src.slice(i + 1, j), next: j + 1 };
    }
    if (src[i] === '\\') {
      var m = /^\\[a-zA-Z]+/.exec(src.slice(i));
      if (m) return { text: m[0], next: i + m[0].length };
    }
    return { text: src.slice(i, i + 1), next: i + 1 };
  }

  function mathRender(src) {
    var out = '', i = 0, s = String(src || '');
    while (i < s.length) {
      var ch = s[i];
      if (ch === '\\') {
        var cmd = /^\\([a-zA-Z]+|\\|,|;|!| )/.exec(s.slice(i));
        if (!cmd) { out += mesc(s[i + 1] || ''); i += 2; continue; }
        var name = cmd[1];
        i += cmd[0].length;
        if (name === ',' || name === ';' || name === ' ' || name === '!') { continue; }
        if (name === '\\') { out += '<br>'; continue; }
        if (name === 'frac' || name === 'tfrac' || name === 'dfrac') {
          var a = groupAt(s, i); i = a.next;
          var b = groupAt(s, i); i = b.next;
          out += '<span class="mfrac"><span class="mfr-n">' + mathRender(a.text) +
            '</span><span class="mfr-d">' + mathRender(b.text) + '</span></span>';
          continue;
        }
        if (name === 'sqrt') {
          var root = '';
          if (s[i] === '[') {
            var close = s.indexOf(']', i);
            root = s.slice(i + 1, close); i = close + 1;
          }
          var g = groupAt(s, i); i = g.next;
          out += '<span class="msqrt">' + (root ? '<span class="msq-i">' + mesc(root) + '</span>' : '') +
            '<span class="msq-s">\u221a</span><span class="msq-r">' + mathRender(g.text) + '</span></span>';
          continue;
        }
        if (name === 'text' || name === 'mathrm' || name === 'operatorname') {
          var t = groupAt(s, i); i = t.next;
          out += '<span class="mtext">' + mesc(t.text) + '</span>';
          continue;
        }
        if (name === 'mathbb') {
          var bb = groupAt(s, i); i = bb.next;
          out += mesc(BB[bb.text.trim()] || bb.text);
          continue;
        }
        if (name === 'vec') {
          var v = groupAt(s, i); i = v.next;
          out += '<span class="mvec">' + mathRender(v.text) + '</span>';
          continue;
        }
        if (name === 'hat' || name === 'bar' || name === 'overline') {
          var o = groupAt(s, i); i = o.next;
          out += '<span class="mover">' + mathRender(o.text) + '</span>';
          continue;
        }
        if (name === 'left' || name === 'right') {
          var br = s[i] === '\\' ? /^\\[a-zA-Z]+/.exec(s.slice(i)) : null;
          if (br) { out += mathRender(s.slice(i, i + br[0].length)); i += br[0].length; }
          else if (s[i] && s[i] !== '.') { out += mesc(s[i]); i += 1; }
          else i += 1;
          continue;
        }
        if (name === 'quad' || name === 'qquad') { out += '<span class="msp' + (name === 'quad' ? '1' : '2') + '"></span>'; continue; }
        if (/^(big|Big|bigl|bigr|Bigl|Bigr|biggl|biggr|displaystyle|limits|nolimits|left|right)$/.test(name)) { continue; }
        if (FUNCS.indexOf(name) >= 0) { out += '<span class="mfn">' + name + '</span>'; continue; }
        if (GREEK[name]) { out += GREEK[name]; continue; }
        if (SYM[name]) { out += name === 'lim' ? '<span class="mfn">lim</span>' : SYM[name]; continue; }
        /* неизвестная команда: показываем без слэша — текст остаётся читаемым */
        out += mesc(name);
        continue;
      }
      if (ch === '^' || ch === '_') {
        var gr = groupAt(s, i + 1); i = gr.next;
        out += ch === '^'
          ? '<sup class="msup">' + mathRender(gr.text) + '</sup>'
          : '<sub class="msub">' + mathRender(gr.text) + '</sub>';
        continue;
      }
      if ('=<>+-*/'.indexOf(ch) >= 0 && ch !== ' ') {
        out += '<span class="mop">' + mesc(ch) + '</span>';
        i += 1;
        continue;
      }
      out += mesc(ch);
      i += 1;
    }
    return out;
  }

  /* УРАВНЕНИЕ или ОБОЗНАЧЕНИЕ? Отдельная строка нужна равенствам/неравенствам
     и многоэтажным конструкциям; короткая запись (\triangle ABC, c^{2},
     \alpha+\beta) остаётся прямо в тексте строки */
  function mathIsBlock(body) {
    var t = String(body || '').trim();
    if (/[=\u2264\u2265\u2260\u2248\u2192\u21d2\u2194]/.test(t)) return true;
    if (/\\(frac|dfrac|sqrt|int|iint|iiint|oint|sum|prod|lim)\b/.test(t)) return true;
    if (t.length > 26 || t.split('\\\\').length > 1) return true;
    return false;
  }

  var MATH_RE = /\\\[([\s\S]*?)(\\\]|$)|\\\(([\s\S]*?)(\\\)|$)/g;

  function render(src) {
    if (!src) return '';
    src = String(src);
    /* СНАЧАЛА ВЫНЕСЕМ ВСЮ математику плейсхолдерами: инлайн-формулы должны
       жить ВНУТРИ абзаца (не разрывать <p>), блочные — вставляться между
       блоками. Плейсхолдеры переживают esc(): в них нет & < > " */
    MATH_RE.lastIndex = 0;
    var m, last = 0, pieces = [], src2 = '';
    while ((m = MATH_RE.exec(src))) {
      if (m.index > last) src2 += src.slice(last, m.index);
      var idx = pieces.length;
      if (m[1] !== undefined) {
        var body = m[1], closed = !!m[2];
        if (mathIsBlock(body)) {
          pieces.push('<div class="math-block' + (closed ? '' : ' math-live') + '">' +
            mathRender(body.trim()) + '</div>');
          src2 += '\n\u0003' + idx + '\u0003\n';
        } else {
          pieces.push('<span class="math-inline' + (closed ? '' : ' math-live') + '">' +
            mathRender(body.trim()) + '</span>');
          src2 += '\u0001' + idx + '\u0002';
        }
      } else {
        pieces.push('<span class="math-inline' + (m[4] ? '' : ' math-live') + '">' +
          mathRender((m[3] || '').trim()) + '</span>');
        src2 += '\u0001' + idx + '\u0002';
      }
      last = MATH_RE.lastIndex;
    }
    if (!pieces.length) return _render(src);
    src2 += src.slice(last);
    var html = _render(src2);
    /* блочный плейсхолдер, попавший в <p>, разворачиваем в блок ДО абзаца */
    html = html.replace(/<p>\s*\u0003(\d+)\u0003\s*<\/p>/g, function (_, i) {
      return pieces[Number(i)];
    });
    html = html.replace(/\u0003(\d+)\u0003/g, function (_, i) { return pieces[Number(i)]; });
    html = html.replace(/\u0001(\d+)\u0002/g, function (_, i) { return pieces[Number(i)]; });
    return html;
  }

  function _render(src) {
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
      var fence = ln.match(/^\s*```+\s*([\wА-Яа-яЁё+-]*)\s*$/);
      if (fence) {
        var lang = (fence[1] || '').toLowerCase();
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        // D. Блок ```ui — не код, а живой элемент управления: слайдеры,
        // переключатели, плитки. Разметку в HTML не превращаем здесь: она
        // перерисовывается на каждом такте печати и стёрла бы состояние.
        // Оставляем спецификацию в data-атрибуте, оживляет её app.js один раз.
        // Любая метка, начинающаяся на «ui», и русские варианты — это панель.
        // Модель пишет то ```ui-panel, то ```UI, то ```интерфейс; ошибка в метке
        // не должна превращать органы управления в мёртвый листинг кода.
        if (/^ui/.test(lang) || lang === 'jarvis-ui' ||
            lang === 'интерфейс' || lang === 'панель' || lang === 'выбор') {
          out.push('<div class="ui-panel" data-ui="' + esc(buf.join('\n')) + '"></div>');
          continue;
        }
        // BG: встроенный математический режим — живой график (2D/3D)
        // и геометрический чертёж прямо в диалоге
        if (lang === 'plot' || lang === 'график' || lang === 'граф') {
          out.push('<div class="plot-panel" data-kind="plot" data-plot="' + esc(buf.join('\n')) + '"></div>');
          continue;
        }
        if (lang === 'geo' || lang === 'геометрия' || lang === 'чертёж' || lang === 'чертеж') {
          out.push('<div class="plot-panel" data-kind="geo" data-plot="' + esc(buf.join('\n')) + '"></div>');
          continue;
        }
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
