Резерв анимации дока/меню (BM19, beta.91).

Если новая анимация (BM20) окажется хуже — вернуть эту:
1. CSS: скопировать app.css из этой папки в app/jarvis/web/css/app.css
2. JS: заменить функцию toggleSidebar в app/jarvis/web/js/app.js
   на toggleSidebar.js из этой папки
3. HTML: убрать обёртки .spaces-in / .spd-in (их добавляет BM20)

Полный слепок репозитория с этой версией: git-тег fold-backup-bm19
(коммит 87bf5f0, beta.91).
