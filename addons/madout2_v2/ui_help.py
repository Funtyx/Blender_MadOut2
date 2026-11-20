# =====================================================
# Сворачиваемая «Помощь» для панелей инструментов MadOut2 V2.
# Строки пишем короткими: label в Blender не переносит текст.
# =====================================================


def draw_help(layout, data, prop_name, lines):
    is_open = getattr(data, prop_name)
    box = layout.box()
    box.prop(data, prop_name, text="Помощь",
             icon='TRIA_DOWN' if is_open else 'TRIA_RIGHT', emboss=False)
    if not is_open:
        return
    col = box.column(align=True)
    col.scale_y = 0.8
    for line in lines:
        col.label(text=line)
