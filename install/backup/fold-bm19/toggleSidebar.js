function toggleSidebar() {
  const app = $('#app');
  if (isNarrow()) { app.classList.toggle('nav-open'); return; }
  app.classList.remove('nav-open');
  const collapsing = !app.classList.contains('collapsed');
  const sp = document.querySelector('.spaces');
  /* BM13: ЧЕСТНАЯ АНИМАЦИЯ ВЫСОТЫ РЯДА. Прежде max-height схлопывался
     классом от выдуманных 132px: пока значение падало от 132 до
     фактических ~40px, ряд стоял неподвижно, а доезжал резко под конец
     — отсюда «однокадровый скачок вверх». Теперь фиксируем фактическую
     высоту инлайном и ведём её к нулю/высоте той же кривой, что меню */
  clearTimeout(_dockedT);
  /* BM19: АНИМАЦИЯ С НУЛЯ — одна кривая/длительность для всех участников
     (CSS --fold-t/--fold-ease + блок .side-folding унификации). JS больше
     не дирижирует разными таймингами: честная высота ряда + один общий
     финал. Ядро и кнопка-стрелка не гаснут, ряд пространств складывается
     высотой — полоса под ним едет непрерывно, без «доезда вниз» */
  if (collapsing) {
    if (sp) {
      sp.style.maxHeight = sp.offsetHeight + 'px';
      void sp.offsetHeight;
    }
    app.classList.add('collapsed', 'side-folding');
    if (sp) requestAnimationFrame(() => { sp.style.maxHeight = '0px'; });
    _dockedT = setTimeout(() => {
      app.classList.add('docked');
      app.classList.remove('side-folding');
    }, 560);
  } else {
    app.classList.remove('docked');
    app.classList.add('side-folding');
    app.classList.remove('collapsed');
    if (sp) {
      sp.style.maxHeight = 'none';
      const h = sp.offsetHeight;
      sp.style.maxHeight = '0px';
      void sp.offsetHeight;
      requestAnimationFrame(() => { sp.style.maxHeight = h + 'px'; });
      setTimeout(() => {
        if (!app.classList.contains('collapsed')) sp.style.maxHeight = '';
      }, 620);
    }
    _dockedT = setTimeout(() => app.classList.remove('side-folding'), 620);
  }
  dockY(collapsing);
  gliderWatchRun();
  /* BK: состояние панели больше не хранится: каждый запуск — с доком */
}
$('#collapseBtn').addEventListener('click', toggleSidebar);
try {
  /* BK: ДЖАРВИС ВСЕГДА ОТКРЫВАЕТСЯ С ДОКОМ. Раньше из localStorage
     восстанавливалось 'open' — достаточно было один раз раскрыть панель,
     и все следующие запуски открывались с боковым меню. Панель — решение
     на ТЕКУЩУЮ сессию: перезапуск всегда возвращает док */
  localStorage.removeItem('jarvis.sidebar2');
  if (!isNarrow()) {
    /* BM12: 'docked' — «превращение завершено»: пространства скрыты,
       без промежуточной анимации при запуске.
       BM18: и СХЛОПНУТЫ по высоте — прежде класс ставился без
       обнуления max-height, невидимый ряд пространств оставлял над
       LIVE пустоту в ~100px */
    $('#app').classList.add('collapsed', 'docked');
    if (sp) sp.style.maxHeight = '0px';
    // восстановление БЕЗ анимации: пилюля сразу в центре высоты
    const dock = document.querySelector('.dock');
    if (dock) {
      dock.style.transition = 'none';
      dockY(true);
      void dock.offsetHeight;
      dock.style.transition = '';
    }
  }
} catch (e) {}
window.addEventListener('resize', () => {
  if ($('#app').classList.contains('collapsed') && !isNarrow()) dockY(true);
});

/* Правой панели больше нет: уведомления, санкции и камера живут прямо в чате
   (см. разделы «камера в диалоге» и «санкции / уведомления в диалоге» ниже). */

/* ============================ переключатели ============================ */
/* КРАСНАЯ ВОЛНА AGENT. Включение агентского режима — не смена галочки, а
   пересадка в гоночный автомобиль: от тумблера к краям экрана мягко
   расходятся красные акценты, и весь интерфейс наливается цветом режима.
   Волна — одноразовый слой поверх всего: расширяется, тает, убирается.
   Класс agent-on на body остаётся и держит красную тему, пока режим жив. */
/* КИСТЬ: волна красит интерфейс ЗА СОБОЙ. Класс темы включается сразу,
   но каждый элемент начинает перекрашиваться ровно тогда, когда фронт
   доходит до него (задержка = расстояние от тумблера / скорость фронта),
   а не весь экран разом. Градиентные фоны браузер не интерполирует — их
   держим старыми до прихода фронта и отпускаем в момент: жёсткая кромка
   кисти. После прохода всё прибирается, элементы живут своей жизнью. */
