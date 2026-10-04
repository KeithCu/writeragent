# In _cell_snapshot:
# add doc or sheet to get the name if we want to format with sheet name.
# Wait, if we use format_address(col, row), it does not include the sheet name.
# So we just do `addr = f"{sheet.getName()}.{format_address(col, row)}"` or something?
# Let's check how addr is formatted if it's on a different sheet.
