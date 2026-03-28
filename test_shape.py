import urllib.request
import re

url = "https://raw.githubusercontent.com/LibreOffice/core/master/svx/source/customshapes/EnhancedCustomShapeTypeNames.cxx"
response = urllib.request.urlopen(url)
data = response.read().decode('utf-8')

types = re.findall(r'"([^"]+)"', data)
# Filter out mso-* and non-lowercase or spaces
clean = sorted([t for t in set(types) if not t.startswith("mso-") and not " " in t and not ":" in t and not t.startswith("col-") and not "=" in t])
print("List of shapes:")
print(', '.join(clean))
