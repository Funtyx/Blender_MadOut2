# MadOut2 Tools для Blender

Все Blender-инструменты MadOut2 в одном аддоне. Вкладка **MadOut2** в боковой панели 3D View (клавиша `N`).

| Панель | Что делает |
|---|---|
| One Side Inset | Односторонний инсет (offset edge loop) |
| Loop Edge & Vertex Color | Резка loop edge и разметка вертекс-колором |
| Clean G-Curb (Live) | Бордюры по выделенным рёбрам, с пресетами |
| Surface Decal | Декаль, проецируемая на поверхность |
| Vertex Color Blend | Перевод материала на смешивание по вертекс-колору |
| Unity Vertex Colors | Мост вертекс-колоров с Unity и экспорт FBX |
| Module Export | Экспорт домов в FBX, а также `.mat`, PNG, префабов и сцены для Unity |
| Обновление | Кнопка «Обновить с GitHub», версия и коммит |

Нужен Blender 4.2 или новее и установленный git.

## Установка (один раз)

### 1. Доступ к репозиторию

Репозиторий приватный. Попросите владельца добавить ваш аккаунт GitHub в репозиторий (Settings → Collaborators) и примите приглашение, которое придёт на почту.

### 2. Вход в GitHub для git

Без этого шага кнопка «Обновить» не сможет скачать обновления.

**macOS:**

1. Установите GitHub CLI: скачайте `.pkg` для macOS со страницы [github.com/cli/cli/releases/latest](https://github.com/cli/cli/releases/latest) и запустите его. Если у вас есть Homebrew, можно проще: `brew install gh`.
2. В Терминале выполните:

```
gh auth login
gh auth setup-git
```

На вопросы `gh auth login` отвечайте: GitHub.com → HTTPS → Login with a web browser. Откроется браузер, нажмите «Authorize».

GitHub Desktop для этого не подходит: он хранит вход только для себя, и обычный git его не видит.

**Windows:** установите [Git for Windows](https://git-scm.com/download/win). Вход в GitHub появится сам при первом клонировании (шаг 3).

### 3. Скачать инструменты

В Терминале (macOS) или Git Bash (Windows):

```
git clone https://github.com/Funtyx/Blender_MadOut2.git
```

Скачать можно в любую папку. Главное — потом её **не перемещать и не удалять**: Blender загружает инструменты прямо из неё.

### 4. Подключить к Blender

Откройте Blender, перейдите на вкладку **Scripting**, нажмите **Open**, выберите `install.py` из скачанной папки и нажмите **Run Script** (▶).

Скрипт установки:
- добавляет папку в *Preferences → File Paths → Script Directories*;
- выключает старые отдельные аддоны (Module Export, One Side Inset и т.д.), иначе они будут конфликтовать с новыми;
- включает **MadOut2 Tools** и сохраняет настройки.

Путь к папке с FBX-мешами в Unity Vertex Colors нужно указать один раз заново.

## Как обновляться

Откройте в Blender **MadOut2 → Обновление** и нажмите **Обновить с GitHub**. Инструменты скачаются и сразу перезагрузятся.

Если появилась ошибка:

| Сообщение | Что делать |
|---|---|
| Нет входа в GitHub | Повторите шаг 2 установки |
| В папке инструментов есть свои правки | Вы что-то меняли в файлах. Откатите свои правки или напишите владельцу |
| Нет связи с GitHub | Проверьте интернет |

## Как выкладывать изменения (тому, кто правит инструменты)

1. Отредактируйте нужный модуль в `addons/madout2_tools/`.
2. В Blender нажмите **MadOut2 → Обновление → Перезагрузить инструменты** и проверьте изменения.
3. Поднимите `version` в `bl_info` в `addons/madout2_tools/__init__.py`.
4. Сделайте коммит и пуш в `main`.

После этого ребята получат изменения по кнопке «Обновить с GitHub».

### Как добавить новый инструмент

1. Положите `.py` с функциями `register()` и `unregister()` в `addons/madout2_tools/`.
2. В `__init__.py` добавьте модуль в `import` и в кортеж `TOOLS`.
3. У панели инструмента поставьте `bl_category = "MadOut2"`, `bl_order = <номер>` и, по желанию, `bl_options = {'DEFAULT_CLOSED'}`.

Настройки аддона (`AddonPreferences`) у пакета могут быть только одни, с `bl_idname = "madout2_tools"`. Сейчас их занимает Unity Vertex Colors. Если новому инструменту тоже нужны настройки, добавьте поля в этот же класс.

## Структура

```
Blender_MadOut2/
├── install.py                  установка одним запуском
├── README.md
└── addons/madout2_tools/       сам аддон (Blender ищет его в addons/)
    ├── __init__.py             регистрация и панель «Обновление»
    ├── one_side_inset.py
    ├── edge_outline.py
    ├── curb.py
    ├── surface_decal.py
    ├── vc_blend.py
    ├── unity_vertex_colors/
    └── module_export.py
```
