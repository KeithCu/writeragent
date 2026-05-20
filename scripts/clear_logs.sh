#!/bin/bash
# Clear WriterAgent debug log in every known location (see plugin/framework/logging.py).
# Primary path: LO user config dir (same folder as writeragent.json).

LO="${HOME}/.config/libreoffice"
rm -f \
  "${LO}/4/user/writeragent_debug.log" \
  "${LO}/4/user/config/writeragent_debug.log" \
  "${LO}/24/user/writeragent_debug.log" \
  "${LO}/24/user/config/writeragent_debug.log"
echo "Logs deleted."
