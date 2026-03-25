import asyncio
import os
import re
import pytz
from datetime import datetime
from playwright.async_api import async_playwright

BB_URL  = os.environ.get("BB_URL",  "https://aulavirtual.up.edu.pe")
BB_USER = os.environ.get("BB_USER", "")
BB_PASS = os.environ.get("BB_PASS", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
async def login(page):
    print("[LOGIN] Abriendo pagina de login...")
    await page.goto(BB_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_selector('#loginid', timeout=30000)

    # Cerrar cualquier overlay/lightbox que bloquee el boton de login
    try:
        overlay = page.locator('div.lb-wrapper[role="dialog"]')
        if await overlay.count() > 0:
            print("[LOGIN] Cerrando dialogo superpuesto...")
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    # Cerrar cookie banners comunes
    try:
        for sel in ['button#onetrust-accept-btn-handler',
                    'button.accept-cookies',
                    'button[aria-label*="Accept"]']:
            el = page.locator(sel)
            if await el.count() > 0:
                await el.click()
                await page.wait_for_timeout(500)
                break
    except Exception:
        pass

    await page.fill('#loginid', BB_USER)
    await page.fill('#pass', BB_PASS)

    # JS click para evitar que overlays intercepten el evento de puntero
    try:
        await page.evaluate("document.querySelector('#entry-login').click()")
    except Exception:
        await page.locator('#pass').press('Enter')

    await page.wait_for_load_state('networkidle', timeout=60000)
    print("[LOGIN] Login completado")

# ---------------------------------------------------------------------------
async def get_all_courses(page):
    print("[CURSOS] Buscando cursos activos...")
    courses = []

    try:
        import json
        api_url = BB_URL.rstrip('/') + '/learn/api/public/v1/courses?availability.available=Yes&fields=id,name,courseId&limit=100'
        resp = await page.evaluate(f"""
            fetch('{api_url}', {{credentials: 'include'}})
              .then(r => r.json())
              .then(d => JSON.stringify(d))
              .catch(e => JSON.stringify({{error: e.toString()}}))
        """)
        data = json.loads(resp)
        if 'results' in data:
            for c in data['results']:
                courses.append({{'id': c['id'], 'name': c.get('name', c.get('courseId', 'Sin nombre'))}})
            print(f"[CURSOS] Encontrados {{len(courses)}} cursos via API")
            return courses
    except Exception as e:
        print(f"[CURSOS] API no disponible: {{e}}")

    try:
        await page.goto(BB_URL.rstrip('/') + '/ultra/stream', wait_until='networkidle', timeout=60000)
        await page.wait_for_timeout(3000)
        links = await page.evaluate("""
            () => {
                const anchors = document.querySelectorAll('a[href*="/ultra/courses/"]');
                const seen = new Set();
                const result = [];
                anchors.forEach(a => {
                    const m = a.href.match(/\/ultra\/courses\/([^/]+)/);
                    if (m && !seen.has(m[1])) {
                        seen.add(m[1]);
                        result.push({id: m[1], name: a.textContent.trim() || m[1]});
                    }
                });
                return result;
            }
        """)
        if links:
            courses = links
            print(f"[CURSOS] Encontrados {{len(courses)}} cursos via scraping")
            return courses
    except Exception as e:
        print(f"[CURSOS] Scraping fallback error: {{e}}")

    print("[CURSOS] No se encontraron cursos")
    return courses

# ---------------------------------------------------------------------------
async def get_gradebook_items(page, course_id, course_name):
    print(f"[GRADEBOOK] Revisando: {{course_name}}")
    items = []
    try:
        url = f"{{BB_URL.rstrip('/')}}/ultra/courses/{{course_id}}/grade/gradebook"
        await page.goto(url, wait_until='networkidle', timeout=60000)
        await page.wait_for_timeout(3000)
        raw = await page.evaluate("""
            () => {
                const result = [];
                const rows = document.querySelectorAll('[class*="gradebook-row"], [class*="grade-row"], tr[data-item-id]');
                rows.forEach(row => {
                    const nameEl  = row.querySelector('[class*="title"], [class*="name"], td:first-child');
                    const scoreEl = row.querySelector('[class*="score"], [class*="grade"], td:nth-child(2)');
                    const dueEl   = row.querySelector('[class*="due"], [class*="date"]');
                    if (nameEl) {
                        const score = scoreEl ? scoreEl.textContent.trim() : '';
                        const pending = !score || score === '-' || score === '';
                        if (pending) result.push({
                            name: nameEl.textContent.trim(),
                            due:  dueEl ? dueEl.textContent.trim() : 'Sin fecha'
                        });
                    }
                });
                return result;
            }
        """)
        items = raw if isinstance(raw, list) else []
        print(f"[GRADEBOOK] {{len(items)}} pendientes en {{course_name}}")
    except Exception as e:
        print(f"[GRADEBOOK] Error en {{course_name}}: {{e}}")
    return items

# ---------------------------------------------------------------------------
def format_report(all_items, total_courses):
    lima_tz = pytz.timezone('America/Lima')
    now     = datetime.now(lima_tz)
    fecha   = now.strftime('%d/%m/%Y %H:%M')
    lines = [
        "<b>📚 Reporte MBA - UP</b>",
        f"<i>{{fecha}} (Lima)</i>",
        f"Cursos revisados: {{total_courses}}",
        ""
    ]
    if not all_items:
        lines.append("✅ <b>No hay tareas pendientes</b> en el gradebook.")
    else:
        for course_name, items in all_items.items():
            if items:
                lines.append(f"\n<b>📖 {{course_name}}</b>")
                for item in items:
                    lines.append(f"  ⏰ {{item.get('name','?')}} — {{item.get('due','Sin fecha')}}")
    lines += ["", "🤖 <i>Bot Alertas MBA - Universidad del Pacífico</i>"]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
def send_telegram(message):
    import urllib.request, urllib.parse, json
    url  = f"https://api.telegram.org/bot{{TELEGRAM_TOKEN}}/sendMessage"
    data = urllib.parse.urlencode({{'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}}).encode()
    req  = urllib.request.Request(url, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
        if res.get('ok'):
            print("[TELEGRAM] Enviado OK")
        else:
            print(f"[TELEGRAM] Error: {{res}}")

# ---------------------------------------------------------------------------
async def main():
    print("=" * 60)
    print("Bot Alertas MBA - Universidad del Pacifico")
    print("=" * 60)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
        )
        context = await browser.new_context(
            viewport={{'width': 1280, 'height': 900}},
            user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
        )
        page = await context.new_page()
        page.set_default_timeout(60000)
        try:
            await login(page)
            courses = await get_all_courses(page)
            if not courses:
                send_telegram("<b>📚 Reporte MBA</b>\n\n⚠️ No se encontraron cursos activos.")
                return
            all_items = {{}}
            for c in courses:
                items = await get_gradebook_items(page, c['id'], c['name'])
                if items:
                    all_items[c['name']] = items
            report = format_report(all_items, len(courses))
            print("\n" + report)
            send_telegram(report)
        except Exception as e:
            import traceback; traceback.print_exc()
            try:
                send_telegram(f"<b>❌ Error Bot MBA</b>\n<code>{{str(e)[:200]}}</code>")
            except Exception:
                pass
            raise
        finally:
            await browser.close()

asyncio.run(main())
