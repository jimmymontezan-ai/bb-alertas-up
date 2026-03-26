import asyncio, os, re, json, pytz, urllib.request, urllib.parse
from datetime import datetime
from playwright.async_api import async_playwright

BB_URL        = os.environ.get("BB_URL", "https://aulavirtual.up.edu.pe")
BB_USER       = os.environ.get("BB_USER", "")
BB_PASS       = os.environ.get("BB_PASS", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
async def login(page):
    print("[LOGIN] Abriendo pagina de login...")
    try:
        await page.goto(BB_URL, wait_until="load", timeout=60000)
    except Exception as e:
        print(f"[LOGIN] goto 'load' fallo ({e}), reintentando...")
        await page.goto(BB_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)
    print(f"[LOGIN] URL: {page.url}")
    print(f"[LOGIN] Titulo: {await page.title()}")
    html_snippet = (await page.content())[:3000]
    print(f"[LOGIN] HTML inicial:\n{html_snippet}")
    username_selector = None
    for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                "input[name='login']", "input[type='text']:visible", "input[autocomplete='username']"]:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                username_selector = sel
                print(f"[LOGIN] Username selector encontrado: {sel}")
                break
        except Exception:
            pass
    if not username_selector:
        direct_login = BB_URL.rstrip("/") + "/webapps/login/"
        print(f"[LOGIN] Navegando a URL directa: {direct_login}")
        try:
            await page.goto(direct_login, wait_until="load", timeout=60000)
        except Exception as e2:
            print(f"[LOGIN] goto direct fallo ({e2})")
            await page.goto(direct_login, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        print(f"[LOGIN] URL directa actual: {page.url}")
        html_snippet2 = (await page.content())[:3000]
        print(f"[LOGIN] HTML directa:\n{html_snippet2}")
        for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                    "input[name='login']", "input[type='text']:visible", "input[autocomplete='username']"]:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    username_selector = sel
                    print(f"[LOGIN] Username selector en directa: {sel}")
                    break
            except Exception:
                pass
    if not username_selector:
        print("[LOGIN] Esperando 15s por renderizado JS...")
        await page.wait_for_timeout(15000)
        html_snippet3 = (await page.content())[:3000]
        print(f"[LOGIN] HTML tras espera:\n{html_snippet3}")
        inputs_info = await page.evaluate(
            "() => Array.from(document.querySelectorAll('input')).map(i => ({id:i.id,name:i.name,type:i.type,placeholder:i.placeholder}))"
        )
        print(f"[LOGIN] Todos los inputs encontrados: {json.dumps(inputs_info)}")
        for sel in ["#loginid", "input[name='user_id']", "input[name='username']",
                    "input[name='login']", "input[type='text']", "input[autocomplete='username']"]:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    username_selector = sel
                    print(f"[LOGIN] Username selector (ultimo intento): {sel}")
                    break
            except Exception:
                pass
    if not username_selector:
        raise Exception("No se encontro formulario de login. Ver HTML en logs.")
    password_selector = None
    for sel in ["#pass", "input[name='password']", "input[type='password']"]:
        try:
            el = page.locator(sel)
            if await el.count() > 0:
                password_selector = sel
                print(f"[LOGIN] Password selector: {sel}")
                break
        except Exception:
            pass
    if not password_selector:
        password_selector = "input[type='password']"
    try:
        ov = page.locator("div.lb-wrapper[role='dialog']")
        if await ov.count() > 0:
            print("[LOGIN] Cerrando overlay...")
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)
    except Exception:
        pass
    await page.fill(username_selector, BB_USER)
    await page.fill(password_selector, BB_PASS)
    submitted = False
    for submit_sel in ["#entry-login", "input[type='submit']", "button[type='submit']",
                       "button:has-text('Iniciar')", "button:has-text('Login')"]:
        try:
            el = page.locator(submit_sel)
            if await el.count() > 0:
                await el.first.click()
                submitted = True
                print(f"[LOGIN] Submit via {submit_sel}")
                break
        except Exception:
            pass
    if not submitted:
        print("[LOGIN] Submit via Enter")
        await page.locator(password_selector).press("Enter")
    await page.wait_for_load_state("networkidle", timeout=60000)
    print(f"[LOGIN] Login completado. URL: {page.url}")

# ---------------------------------------------------------------------------
async def get_all_courses(page):
    print("[CURSOS] Buscando cursos...")
    courses = []
    try:
        api_url = BB_URL.rstrip("/") + "/learn/api/public/v1/courses?availability.available=Yes&fields=id,name,courseId&limit=100"
        resp = await page.evaluate(
            "fetch('" + api_url.replace("'", "\\'") + "', {credentials:'include'}).then(r=>r.json()).then(d=>JSON.stringify(d)).catch(e=>JSON.stringify({error:e.toString()}))"
        )
        data = json.loads(resp)
        if "results" in data:
            for c in data["results"]:
                courses.append({"id": c["id"], "name": c.get("name", c.get("courseId", "?"))})
        print(f"[CURSOS] {len(courses)} cursos via API")
        return courses
    except Exception as e:
        print(f"[CURSOS] API error: {e}")
    try:
        await page.goto(BB_URL.rstrip("/") + "/ultra/stream", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        links = await page.evaluate(
            """() => {
                const a = document.querySelectorAll('a[href*="/ultra/courses/"]');
                const seen = new Set();
                const res = [];
                a.forEach(el => {
                    const m = el.href.match(/\\/ultra\\/courses\\/([^/]+)/);
                    if (m && !seen.has(m[1])) {
                        seen.add(m[1]);
                        res.push({id:m[1], name:el.textContent.trim()||m[1]});
                    }
                });
                return res;
            }"""
        )
        if links:
            courses = links
        print(f"[CURSOS] {len(courses)} cursos via scraping")
    except Exception as e:
        print(f"[CURSOS] scraping error: {e}")
    return courses

# ---------------------------------------------------------------------------
async def get_gradebook_items(page, course_id, course_name):
    print(f"[GRADEBOOK] {course_name}")
    items = []
    try:
        url = BB_URL.rstrip("/") + "/ultra/courses/" + course_id + "/grade/gradebook"
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(3000)
        raw = await page.evaluate(
            """() => {
                const rows = document.querySelectorAll('[class*="gradebook-row"],[class*="grade-row"],tr[data-item-id]');
                const res = [];
                rows.forEach(row => {
                    const n = row.querySelector('[class*="title"],[class*="name"],td:first-child');
                    const s = row.querySelector('[class*="score"],[class*="grade"],td:nth-child(2)');
                    const d = row.querySelector('[class*="due"],[class*="date"]');
                    if (n) {
                        const sc = s ? s.textContent.trim() : "";
                        if (!sc || sc === "-") res.push({name:n.textContent.trim(), due:d?d.textContent.trim():"Sin fecha"});
                    }
                });
                return res;
            }"""
        )
        items = raw if isinstance(raw, list) else []
        print(f"[GRADEBOOK] {len(items)} pendientes")
    except Exception as e:
        print(f"[GRADEBOOK] error: {e}")
    return items

# ---------------------------------------------------------------------------
def format_report(all_items, total_courses):
    lima_tz = pytz.timezone("America/Lima")
    now = datetime.now(lima_tz)
    fecha = now.strftime("%d/%m/%Y %H:%M")
    lines = ["<b>📚 Reporte MBA - UP</b>", f"<i>{fecha} (Lima)</i>",
             f"Cursos revisados: {total_courses}", ""]
    if not all_items:
        lines.append("✅ <b>No hay tareas pendientes.</b>")
    else:
        for cname, items in all_items.items():
            if items:
                lines.append(f"\n<b>📖 {cname}</b>")
                for it in items:
                    lines.append(f"  ⏰ {it.get('name','?')} — {it.get('due','Sin fecha')}")
    lines += ["", "🤖 <i>Bot Alertas MBA - UP</i>"]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
def send_telegram(message):
    if not TELEGRAM_TOKEN:
        print("[TELEGRAM] ERROR: TELEGRAM_TOKEN vacio! Verificar secretos de GitHub.")
        raise ValueError("TELEGRAM_TOKEN no configurado")
    if not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] ERROR: TELEGRAM_CHAT_ID vacio! Verificar secretos de GitHub.")
        raise ValueError("TELEGRAM_CHAT_ID no configurado")
    print(f"[TELEGRAM] Token len={len(TELEGRAM_TOKEN)}, inicio={TELEGRAM_TOKEN[:10]}..., ChatID={TELEGRAM_CHAT_ID}")
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
        print("[TELEGRAM] OK" if res.get("ok") else f"[TELEGRAM] Error: {res}")
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f"[TELEGRAM] HTTP {e.code} {e.reason}: {body}")
        raise

# ---------------------------------------------------------------------------
async def main():
    print("=" * 60)
    print("Bot Alertas MBA - Universidad del Pacifico")
    print("=" * 60)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        page.set_default_timeout(60000)
        try:
            await login(page)
            courses = await get_all_courses(page)
            if not courses:
                send_telegram("<b>📚 Reporte MBA</b>\n\n⚠️ No se encontraron cursos.")
                return
            all_items = {}
            for c in courses:
                items = await get_gradebook_items(page, c["id"], c["name"])
                if items:
                    all_items[c["name"]] = items
            report = format_report(all_items, len(courses))
            print(report)
            send_telegram(report)
        except Exception as e:
            import traceback; traceback.print_exc()
            try:
                send_telegram(f"<b>❌ Error Bot MBA</b>\n<code>{str(e)[:200]}</code>")
            except Exception:
                pass
            raise
        finally:
            await browser.close()

asyncio.run(main())
