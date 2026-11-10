# MadOut2 Tools для Blender

Все инструменты MadOut2 на одной вкладке **MadOut2** в боковой панели 3D View (клавиша `N`). Нужен Blender 4.2 или новее.

## Установка

1. Откройте **Edit → Preferences → Get Extensions**. Если Blender попросит, нажмите **Allow Online Access**.
2. Справа вверху нажмите **Repositories** → **+** → **Add Remote Repository**.
3. В поле **URL** вставьте:
   ```
   https://raw.githubusercontent.com/Funtyx/Blender_MadOut2/main/repo/index.json
   ```
   Включите **Authentication** и в поле **Access Token** вставьте токен, который вам дали.
4. Найдите в списке **MadOut2 Tools** и нажмите **Install**.

Обновления Blender проверяет сам: когда выйдет новая версия, рядом с аддоном появится кнопка **Update**.

## Выпуск новой версии

1. Поднимите `version` в `addons/madout2_tools/blender_manifest.toml`.
2. Выполните `python3 publish.py`.
3. Закоммитьте и запушьте изменения вместе с папкой `repo/`.
