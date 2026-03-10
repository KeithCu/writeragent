import json
import time
from playwright.sync_api import sync_playwright

def get_model_slugs(page, url):
    page.goto(url)
    try:
        page.wait_for_selector('a[href*="/"] h3', timeout=20000)
    except:
        pass
    
    # Scroll a bit to ensure all lazy loaded cards appear
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(1)
        
    cards = page.locator('a[href*="/"]').all()
    slugs = set()
    for card in cards:
        href = card.get_attribute('href')
        if href and href.startswith('/') and len(href.split('/')) == 3:
            # typical format is /provider/model
            slug = href.lstrip('/')
            slugs.add(slug)
    return slugs

def scrape_model_details(page, slug):
    try:
        page.goto(f"https://openrouter.ai/{slug}", timeout=15000)
        try:
            page.wait_for_selector('h1', timeout=5000)
            name = page.locator('h1').inner_text().strip()
        except:
            name = slug # Fallback to slug if h1 isn't found
            
        text = page.locator('body').inner_text()
        
        lines = text.split('\n')
        price = "N/A"
        for line in lines:
            line_lower = line.lower()
            if '/m tokens' in line_lower and '$' in line_lower and 'input' not in line_lower and 'output' not in line_lower:
                price = line.strip()
                break
                
        # Some models use different formatting, e.g., "$0.15 / 1K output image"
        if price == "N/A":
            for line in reversed(lines):
                line_lower = line.lower()
                if '$' in line_lower and ('image' in line_lower or 'output' in line_lower) and 'input' not in line_lower:
                    price = line.strip()
                    break
        
        return name, price
    except Exception as e:
        return slug, "N/A"

def list_image_models():
    print("Scraping image models directly from OpenRouter UI (API is incomplete)...")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print("Fetching all image generation models...")
        all_slugs = get_model_slugs(page, 'https://openrouter.ai/models?output_modalities=image')
        
        print("Fetching image editing models...")
        editing_slugs = get_model_slugs(page, 'https://openrouter.ai/models?input_modalities=image&output_modalities=image')
        
        # Combine them (in case editing has something not in all, though unlikely)
        all_slugs = all_slugs.union(editing_slugs)
        
        # Remove false positives (like links that aren't providers)
        valid_slugs = [s for s in all_slugs if s not in ['docs/api-reference', 'docs/quick-start', 'docs/models']]
        
        print(f"Found {len(valid_slugs)} total image models to process.")
        
        final_list = []
        for i, slug in enumerate(valid_slugs):
            print(f"[{i+1}/{len(valid_slugs)}] Scraping details for {slug}...")
            name, price = scrape_model_details(page, slug)
            supports_editing = slug in editing_slugs
            
            final_list.append({
                "id": slug,
                "name": name,
                "supports_editing": supports_editing,
                "image_token_price": price
            })
            
        browser.close()
        
    print(f"\nDone! Found {len(final_list)} total image models. ({len(editing_slugs)} support editing)")
    
    for m in final_list:
        print(f"- {m['name']} ({m['id']}) | Supports Editing: {m['supports_editing']}\n  Image Token Price: {m['image_token_price']}\n")
        
    with open("openrouterconfig.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, indent=4)
        
    print("Wrote output to openrouterconfig.json")

if __name__ == "__main__":
    list_image_models()
