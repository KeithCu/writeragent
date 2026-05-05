# Copyright (c) David Berlioz
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Print tool for all document types via XPrintable."""

from plugin.framework.tool_base import ToolBaseDummy


class PrintDocument(ToolBaseDummy):
    """Print the current document."""

    name = "print_document"
    intent = "media"
    description = "Print the current document to the default printer or a named printer. Can also print to PDF via printer name. Works on all document types."
    parameters = {
        "type": "object",
        "properties": {
            "printer": {"type": "string", "description": "Printer name (default printer if omitted)."},
            "pages": {"type": "string", "description": ("Page range to print (e.g. '1-3', '1,3,5'). All pages if omitted.")},
            "copies": {"type": "integer", "description": "Number of copies (default: 1)."},
        },
        "required": [],
    }
    uno_services = None  # all document types
    is_mutation = False

    def execute(self, ctx, **kwargs):
        from com.sun.star.beans import PropertyValue

        doc = ctx.doc
        printer_name = kwargs.get("printer")
        pages = kwargs.get("pages")
        copies = kwargs.get("copies", 1)

        try:
            # Set printer if specified
            if printer_name:
                printer_props = (PropertyValue(Name="Name", Value=printer_name),)
                doc.setPrinter(printer_props)

            # Build print options
            opts = []
            if pages:
                opts.append(PropertyValue(Name="Pages", Value=pages))
            if copies and copies > 1:
                opts.append(PropertyValue(Name="CopyCount", Value=copies))
            opts.append(PropertyValue(Name="Wait", Value=True))

            doc.print(tuple(opts))

            return {"status": "ok", "message": "Document sent to printer.", "printer": printer_name or "(default)", "pages": pages or "all", "copies": copies}
        except Exception as e:
            return self._tool_error(str(e))
