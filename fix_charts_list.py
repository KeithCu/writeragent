with open('plugin/calc/charts.py', 'r') as f:
    content = f.read()


search = """
        if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
            try:
                sheets = doc.getSheets()
                for i in range(sheets.getCount()):
                    sheet = sheets.getByIndex(i)
                    sheet_name = sheet.getName()
                    charts = sheet.getCharts()
                    for name in charts.getElementNames():
                        chart_obj = charts.getByName(name)
                        result.append(self._get_summary(chart_obj, name, sheet_name=sheet_name))
            except Exception:
                pass

        elif supportsService(doc, "com.sun.star.text.TextDocument"):
            objects = doc.getEmbeddedObjects()
            for name in objects.getElementNames():
                obj = objects.getByName(name)
                if _writer_embed_is_chart(obj):
                    result.append(self._get_summary(obj, name))
            try:
                page = doc.getDrawPage()
                for j in range(page.getCount()):
                    shape = _ole2_shape_at(page, j)
                    if shape is None:
                        continue
                    name = getattr(shape, "Name", None) or f"Shape_{j}"
                    result.append(self._get_summary(shape, name))
            except Exception:
                pass
"""

replace = """
        if supportsService(doc, "com.sun.star.sheet.SpreadsheetDocument"):
            try:
                sheets = doc.getSheets()
                for i in range(sheets.getCount()):
                    sheet = sheets.getByIndex(i)
                    sheet_name = sheet.getName()
                    charts = sheet.getCharts()
                    for name in charts.getElementNames():
                        chart_obj = charts.getByName(name)
                        result.append(self._get_summary(chart_obj, name, sheet_name=sheet_name))
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise

        elif supportsService(doc, "com.sun.star.text.TextDocument"):
            objects = doc.getEmbeddedObjects()
            for name in objects.getElementNames():
                obj = objects.getByName(name)
                if _writer_embed_is_chart(obj):
                    result.append(self._get_summary(obj, name))
            try:
                page = doc.getDrawPage()
                for j in range(page.getCount()):
                    shape = _ole2_shape_at(page, j)
                    if shape is None:
                        continue
                    name = getattr(shape, "Name", None) or f"Shape_{j}"
                    result.append(self._get_summary(shape, name))
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise
"""

content = content.replace(search, replace)

search2 = """
        elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
            try:
                for i in range(doc.getDrawPages().getCount()):
                    page = doc.getDrawPages().getByIndex(i)
                    for j in range(page.getCount()):
                        shape = _ole2_shape_at(page, j)
                        if shape is None:
                            continue
                        name = getattr(shape, "Name", None) or f"Shape_{j}"
                        result.append(self._get_summary(shape, name, sheet_name=f"Page {i+1}"))
            except Exception:
                pass
"""

replace2 = """
        elif supportsService(doc, "com.sun.star.drawing.DrawingDocument") or supportsService(doc, "com.sun.star.presentation.PresentationDocument"):
            try:
                for i in range(doc.getDrawPages().getCount()):
                    page = doc.getDrawPages().getByIndex(i)
                    for j in range(page.getCount()):
                        shape = _ole2_shape_at(page, j)
                        if shape is None:
                            continue
                        name = getattr(shape, "Name", None) or f"Shape_{j}"
                        result.append(self._get_summary(shape, name, sheet_name=f"Page {i+1}"))
            except Exception as exc:
                if is_disposed_exception(exc):
                    raise
"""

content = content.replace(search2, replace2)

with open('plugin/calc/charts.py', 'w') as f:
    f.write(content)
