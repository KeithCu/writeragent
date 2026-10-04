import sys
import os
sys.path.insert(0, os.path.abspath("."))
import plugin.calc.error_detector as ed
from plugin.calc.address_utils import split_sheet_prefix, parse_address

def new_explain_error(self, address: str):
    pass

print("tested syntax")
