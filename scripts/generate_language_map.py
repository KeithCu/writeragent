import matplotlib.pyplot as plt
import geopandas as gpd
import pandas as pd

# List of supported language codes
langs = [
    "de", "es", "fr", "it", "ja", "ko", "pl", "pt", "ru", "zh_CN", "zh_TW", 
    "hi_IN", "id", "bn_IN", "ur_PK", "nl", "sv", "da", "fi", "ca", "el", 
    "cs", "hu", "ro", "nb_NO", "nn_NO", "uk", "tr", "sk", "bg", "hr", 
    "lt", "lv", "et"
]

# Mapping language codes to ISO 3166-1 alpha-3 country codes
# This covers primary regions for the provided languages
iso_mapping = {
    "aq": ["ATA"],  # 'aq' is the country code for Antarctica
    "en": ["USA", "GBR", "CAN", "AUS", "NZL", "IRL", "ZAF", "NGA", "PHL", "JAM"],
    "de": ["DEU", "AUT", "CHE", "LIE", "LUX"],
    "es": ["ESP", "MEX", "ARG", "COL", "CHL", "PER", "VEN", "ECU", "GTM", "CUB", "BOL", "DOM", "HND", "PRY", "SLV", "NIC", "CRC", "PAN", "URY"],
    "fr": ["FRA", "CAN", "BEL", "CHE", "LUX", "COD", "MDG", "CMR", "CIV", "NER", "SEN", "MLI"],
    "it": ["ITA", "CHE"],
    "ja": ["JPN"],
    "ko": ["KOR"],
    "pl": ["POL"],
    "pt": ["PRT", "BRA", "AGO", "MOZ"],
    "ru": ["RUS", "BLR", "KAZ"],
    "zh_CN": ["CHN", "SGP"],
    "zh_TW": ["TWN"],
    "hi_IN": ["IND"],
    "id": ["IDN"],
    "bn_IN": ["IND", "BGD"],
    "ur_PK": ["PAK"],
    "nl": ["NLD", "BEL", "SUR"],
    "sv": ["SWE", "FIN"],
    "da": ["DNK"],
    "fi": ["FIN"],
    "ca": ["ESP", "AND"],
    "el": ["GRC", "CYP"],
    "cs": ["CZE"],
    "hu": ["HUN"],
    "ro": ["ROU", "MDA"],
    "nb_NO": ["NOR"],
    "nn_NO": ["NOR"],
    "uk": ["UKR"],
    "tr": ["TUR", "CYP"],
    "sk": ["SVK"],
    "bg": ["BGR"],
    "hr": ["HRV"],
    "lt": ["LTU"],
    "lv": ["LVA"],
    "et": ["EST"]
}

# Flatten the supported ISO list
supported_iso = {code for codes in iso_mapping.values() for code in codes}

# Load world map (GeoJSON format is usually more reliable)
world_url = "https://raw.githubusercontent.com/datasets/geo-boundaries-world-110m/master/countries.geojson"
world = gpd.read_file(world_url)

# Apply coverage status
world['status'] = world['iso_a3'].apply(lambda x: 'Supported' if x in supported_iso else 'Uncovered')

# Plotting
fig, ax = plt.subplots(1, 1, figsize=(16, 8))
world.plot(
    column='status',
    ax=ax,
    legend=True,
    color=world['status'].map({'Supported': '#2ca02c', 'Uncovered': '#d62728'}),
    edgecolor='white',
    linewidth=0.2
)

ax.set_title('Plugin Language Coverage: Supported vs Uncovered', fontsize=18, pad=20)
ax.axis('off')

plt.tight_layout()
plt.savefig('language_coverage_map.png', dpi=300)