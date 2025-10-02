#!/usr/bin/env python3
"""
webpage_scraper.py

Dark-themed article scraper GUI using CustomTkinter:
- Paste URL (clipboard / Paste button / right-click / Ctrl+V)
- Extracts title, author, date, headings, paragraphs, images + captions
- Save as PDF (images embedded) and/or TXT
- Modern dark UI with CustomTkinter buttons
- Opens PDF after creation (cross-platform)
- Uses readability-lxml if installed; falls back to heuristics otherwise
- Optionally renders JS-heavy pages with Playwright (if installed)
"""

import os
import sys
import tempfile
import threading
import subprocess
from urllib.parse import urljoin, urlparse

# GUI
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Networking & parsing
import requests
from bs4 import BeautifulSoup
from PIL import Image


# PDF
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Optional readability
try:
    from readability import Document as ReadabilityDocument
    HAS_READABILITY = True
except Exception:
    HAS_READABILITY = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}

# ---------------------------
# Fetch HTML (requests + optional Playwright)
# ---------------------------
def fetch_html(url, timeout=20, try_playwright_if_needed=True):
    session = requests.Session()
    text, final_url = None, url
    try:
        resp = session.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        text, final_url = resp.text, resp.url
    except Exception:
        text = None

    js_heavy_sites = ("tesla.com", "twitter.com", "instagram.com", "facebook.com", "youtube.com")
    need_render = False
    if text is None or len(text.strip()) < 800:
        need_render = True
    elif "<noscript" in (text or "").lower() or "javascript required" in (text or "").lower():
        need_render = True
    try:
        domain = urlparse(url).netloc.lower()
        if any(d in domain for d in js_heavy_sites):
            need_render = True
    except Exception:
        pass

    if need_render and try_playwright_if_needed:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_default_navigation_timeout(timeout * 1000)
                page.goto(url, wait_until="networkidle")
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
                rendered, final = page.content(), page.url
                browser.close()
                if rendered and len(rendered.strip()) > 200:
                    return rendered, final
        except Exception:
            pass

    return (text or ""), (final_url or url)

# ---------------------------
# Extraction utilities
# ---------------------------
def extract_more_elements(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    out = {}
    out['title'] = (soup.title.string.strip() if soup.title and soup.title.string else "") or ""

    # author
    author = None
    for key in ('author','article:author','og:article:author','byline'):
        tag = soup.find("meta", {"name": key}) or soup.find("meta", {"property": key})
        if tag and tag.get("content"):
            author = tag.get("content").strip(); break
    if not author:
        sel = soup.select_one('[rel=author]') or soup.select_one('.author') or soup.select_one('.byline') or soup.select_one('[itemprop=author]')
        if sel:
            author = sel.get_text(strip=True)
    out['author'] = author or ""

    # date/time
    date = None
    for key in ('article:published_time','pubdate','publishdate','date'):
        tag = soup.find("meta", {"name": key}) or soup.find("meta", {"property": key})
        if tag and tag.get("content"):
            date = tag.get("content").strip(); break
    if not date:
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            date = time_tag.get("datetime").strip()
        elif time_tag:
            date = time_tag.get_text(strip=True)
    out['date'] = date or ""

    # tags/keywords
    keywords = None
    tagm = soup.find("meta", {"name": "keywords"})
    if tagm and tagm.get("content"):
        keywords = tagm.get("content").strip()
    out['tags'] = keywords or ""

    # lead image (og:image fallback)
    og = soup.find("meta", {"property": "og:image"}) or soup.find("meta", {"name": "og:image"})
    lead_image = None
    if og and og.get("content"):
        lead_image = urljoin(base_url, og.get("content"))
    else:
        imgs = soup.find_all("img")
        best = None
        for img in imgs[:30]:
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if not src:
                continue
            best = urljoin(base_url, src)
            break
        lead_image = best
    out['lead_image'] = lead_image or ""

    # Try readability for main content if available
    main_content = None
    if HAS_READABILITY:
        try:
            doc = ReadabilityDocument(html)
            summary_html = doc.summary()
            content_soup = BeautifulSoup(summary_html, "lxml")
            main_content = content_soup
            title_r = doc.short_title()
            if title_r:
                out['title'] = title_r
        except Exception:
            main_content = None

    if not main_content:
        candidate = soup.find('article') or soup.find('main')
        if not candidate:
            candidates = soup.find_all(['div','section'], recursive=True)
            best = soup.body or soup
            max_len = 0
            for c in candidates:
                txt = c.get_text(separator=" ", strip=True)
                if len(txt) > max_len:
                    max_len = len(txt)
                    best = c
            candidate = best
        main_content = candidate

    # Build ordered nodes
    nodes = []
    for el in main_content.descendants:
        if getattr(el, "name", None) in ("h1","h2","h3","h4"):
            t = el.get_text(strip=True)
            if t:
                nodes.append(("heading", t, el.name))
        elif getattr(el, "name", None) in ("p","blockquote","li"):
            t = el.get_text(strip=True)
            if t and len(t) > 10:
                nodes.append(("paragraph", t, None))
        elif getattr(el, "name", None) == "img":
            src = el.get("src") or el.get("data-src") or el.get("data-original")
            if src:
                absu = urljoin(base_url, src)
                caption = ""
                if el.parent:
                    cap = el.parent.find("figcaption")
                    if cap:
                        caption = cap.get_text(strip=True)
                if not caption:
                    caption = el.get("alt") or ""
                nodes.append(("image", absu, caption))
    out['nodes'] = nodes
    return out

# ---------------------------
# PDF helpers (register font + create PDF)
# ---------------------------
def register_dejavu():
    possible = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/local/share/fonts/DejaVuSans.ttf",
        os.path.join(os.path.expanduser("~"), ".local", "share", "fonts", "DejaVuSans.ttf"),
    ]
    for p in possible:
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont("DejaVuSans", p))
                return "DejaVuSans"
            except Exception:
                pass
    # fallback to Helvetica (reportlab default)
    return "Helvetica"

def download_image_local(url, session, tmpdir):
    try:
        r = session.get(url, headers=HEADERS, stream=True, timeout=20)
        r.raise_for_status()
        ct = r.headers.get("content-type","")
        ext = ".jpg"
        if "png" in ct:
            ext = ".png"
        elif "gif" in ct:
            ext = ".gif"
        path = os.path.join(tmpdir, f"img_{abs(hash(url)) % (10**9)}{ext}")
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return path
    except Exception:
        return None

def split_text_to_lines(text, fontname, fontsize, max_width):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        candidate = (cur + " " + w).strip()
        if stringWidth(candidate, fontname, fontsize) <= max_width:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def create_pdf(path, meta, nodes, pagesize=A4):
    PAGE_W, PAGE_H = pagesize
    margin = 28
    usable_w = PAGE_W - 2*margin
    fontname = register_dejavu()
    c = canvas.Canvas(path, pagesize=pagesize)
    x = margin; y = PAGE_H - margin

    # Title
    if meta.get('title'):
        c.setFont(fontname, 18)
        title_lines = split_text_to_lines(meta['title'], fontname, 18, usable_w)
        for line in title_lines:
            if y < margin + 40:
                c.showPage(); y = PAGE_H - margin
            c.drawString(x, y, line)
            y -= 22
        y -= 6

    # Meta (author/date/tags)
    info = []
    if meta.get('author'):
        info.append("By " + meta.get('author'))
    if meta.get('date'):
        info.append(meta.get('date'))
    if meta.get('tags'):
        info.append("Tags: " + meta.get('tags'))
    if info:
        c.setFont(fontname, 9)
        line = "  |  ".join(info)
        if y < margin + 20:
            c.showPage(); y = PAGE_H - margin
        c.drawString(x, y, line)
        y -= 16; y -= 6

    session = requests.Session()
    tmpdir = tempfile.mkdtemp(prefix="scrape_imgs_")

    for node in nodes:
        typ = node[0]
        if typ == "heading":
            txt = node[1]; size = 14 if node[2] == "h1" else 12
            c.setFont(fontname, size)
            lines = split_text_to_lines(txt, fontname, size, usable_w)
            for ln in lines:
                if y < margin + size*2:
                    c.showPage(); y = PAGE_H - margin
                c.drawString(x, y, ln)
                y -= size + 2
            y -= 6
        elif typ == "paragraph":
            txt = node[1]; size = 11
            c.setFont(fontname, size)
            lines = split_text_to_lines(txt, fontname, size, usable_w)
            for ln in lines:
                if y < margin + size*2:
                    c.showPage(); y = PAGE_H - margin
                c.drawString(x, y, ln)
                y -= size + 2
            y -= 8
        elif typ == "image":
            img_url = node[1]; caption = node[2] or ""
            local = download_image_local(img_url, session, tmpdir)
            if local:
                try:
                    img = Image.open(local)
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                    iw, ih = img.size
                    display_w = min(usable_w, iw)
                    display_h = display_w * (ih / iw)
                    if y - display_h < margin:
                        c.showPage(); y = PAGE_H - margin
                    img_reader = ImageReader(img)
                    c.drawImage(img_reader, x, y - display_h, width=display_w, height=display_h, preserveAspectRatio=True, anchor='sw')
                    y -= display_h + 6
                    if caption:
                        c.setFont(fontname, 9)
                        cap_lines = split_text_to_lines(caption, fontname, 9, usable_w)
                        for cl in cap_lines:
                            if y < margin + 12:
                                c.showPage(); y = PAGE_H - margin
                            c.drawString(x + 4, y, cl)
                            y -= 11
                        y -= 6
                except Exception:
                    pass

    c.save()
    return path

# ---------------------------
# Cross-platform open file
# ---------------------------
def open_file(path):
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        pass

# ---------------------------
# GUI (CustomTkinter) — padding applied in pack/grid, not in constructors
# ---------------------------
def apply_dark_style(root):
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("dark-blue")
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    root.configure(bg="#1e1e2a")

class ScraperGUI:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.title("Article Scraper — Dark")
        self.root.geometry("950x560")
        apply_dark_style(self.root)

        # Main container
        main = ctk.CTkFrame(self.root, fg_color="#252536", corner_radius=0)
        main.pack(fill="both", expand=True, padx=12, pady=12)

        # Top header
        top = ctk.CTkFrame(main, fg_color="#1e1e2a", corner_radius=0)
        top.pack(fill="x", pady=(0,8))
        title_label = ctk.CTkLabel(top, text="Article → PDF / TXT", text_color="#d4d4dc", font=("Segoe UI", 16, "bold"))
        title_label.pack(side="left", padx=(8, 8), pady=6)
        subtitle = ctk.CTkLabel(top, text="(paste a URL below)", text_color="#9a9aa6", font=("Segoe UI", 10))
        subtitle.pack(side="left", pady=6)

        # Card (url + options)
        card = ctk.CTkFrame(main, fg_color="#252536", corner_radius=8)
        card.pack(fill="x", padx=4, pady=(0,8))

        self.url_var = tk.StringVar()
        self.entry = ctk.CTkEntry(card, textvariable=self.url_var, width=860, height=36, placeholder_text="https://")
        self.entry.pack(fill="x", padx=12, pady=(12,8))
        self.entry.focus()

        row = ctk.CTkFrame(card, fg_color="#252536", corner_radius=0)
        row.pack(fill="x", padx=12, pady=(0,12))

        self.output_path = tk.StringVar(value=os.path.join(os.getcwd(), "article_output.pdf"))
        self.save_pdf_var = tk.BooleanVar(value=True)
        self.save_txt_var = tk.BooleanVar(value=False)

        # NOTE: CustomTkinter uses CTkCheckBox (capital B)
        chk_pdf = ctk.CTkCheckBox(row, text="Save as PDF", variable=self.save_pdf_var)
        chk_pdf.pack(side="left", padx=6)
        chk_txt = ctk.CTkCheckBox(row, text="Save as TXT", variable=self.save_txt_var)
        chk_txt.pack(side="left", padx=6)

        paste_btn = ctk.CTkButton(row, text="Paste URL", command=self._paste_and_focus, width=120, height=36)
        paste_btn.pack(side="left", padx=8)
        choose_btn = ctk.CTkButton(row, text="Choose Output", command=self.choose_output, width=140, height=36)
        choose_btn.pack(side="left", padx=6)

        self.scrape_btn = ctk.CTkButton(row, text="Scrape & Save", command=self.on_scrape, width=180, height=44)
        self.scrape_btn.pack(side="right")

        # Bottom: left (preview/status) + right (metadata)
        bottom = ctk.CTkFrame(main, fg_color="#1e1e2a", corner_radius=0)
        bottom.pack(fill="both", expand=True, padx=4, pady=(6,0))

        left = ctk.CTkFrame(bottom, fg_color="#1e1e2a", corner_radius=0)
        left.pack(side="left", fill="both", expand=True, padx=(0,8))

        status_lbl = ctk.CTkLabel(left, text="Status", text_color="#9a9aa6")
        status_lbl.pack(anchor="w", padx=6, pady=(6,0))
        self.status_var = tk.StringVar(value="Ready")
        status_val = ctk.CTkLabel(left, textvariable=self.status_var, text_color="#9a9aa6")
        status_val.pack(anchor="w", padx=6, pady=(2,8))

        preview_lbl = ctk.CTkLabel(left, text="Preview (first 1200 chars)", text_color="#9a9aa6")
        preview_lbl.pack(anchor="w", padx=6)
        self.preview = tk.Text(left, height=18, bg="#17171b", fg="#d4d4dc", insertbackground="#d4d4dc", wrap="word", relief="flat")
        self.preview.pack(fill="both", expand=True, padx=6, pady=(6,6))

        right = ctk.CTkFrame(bottom, fg_color="#1e1e2a", corner_radius=0)
        right.pack(side="right", fill="y", padx=6)
        meta_lbl = ctk.CTkLabel(right, text="Metadata", text_color="#9a9aa6")
        meta_lbl.pack(anchor="w", padx=6, pady=(6,0))
        self.meta_box = tk.Text(right, height=20, width=42, bg="#17171b", fg="#d4d4dc", relief="flat")
        self.meta_box.pack(fill="both", padx=6, pady=(6,6))

    def _paste_and_focus(self):
        try:
            txt = self.root.clipboard_get()
            self.url_var.set(txt.strip())
        except Exception:
            pass
        self.entry.focus_set()

    def choose_output(self):
        p = filedialog.asksaveasfilename(defaultextension=".pdf",
                                         filetypes=[("PDF","*.pdf"),("Text","*.txt")],
                                         initialfile=os.path.basename(self.output_path.get()))
        if p:
            self.output_path.set(p)

    def set_status(self, s):
        self.status_var.set(s)
        self.root.update_idletasks()

    def on_scrape(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Please paste a URL first.")
            return
        if not (self.save_pdf_var.get() or self.save_txt_var.get()):
            messagebox.showwarning("No output selected", "Choose at least one: Save as PDF or Save as TXT.")
            return
        out_path = self.output_path.get().strip()
        if not out_path:
            messagebox.showwarning("No output file", "Please choose an output filename.")
            return

        # disable button (CTk widgets use configure)
        try:
            self.scrape_btn.configure(state="disabled")
        except Exception:
            try:
                self.scrape_btn.config(state="disabled")
            except Exception:
                pass

        self.set_status("Fetching article...")
        threading.Thread(target=self._worker, args=(url, out_path), daemon=True).start()

    def _worker(self, url, out_path):
        try:
            html, final = fetch_html(url)
            self.set_status("Extracting article elements...")
            meta = extract_more_elements(html, final)

            # Build plain text
            text_parts = []
            if meta.get('title'):
                text_parts.append(meta['title']); text_parts.append("")
            if meta.get('author'):
                text_parts.append("By " + meta['author'])
            if meta.get('date'):
                text_parts.append("Published: " + meta['date'])
            if meta.get('tags'):
                text_parts.append("Tags: " + meta['tags'])
            text_parts.append("")

            for node in meta.get('nodes', []):
                if node[0] == 'heading':
                    text_parts.append(node[1].upper())
                elif node[0] == 'paragraph':
                    text_parts.append(node[1])
                elif node[0] == 'image':
                    text_parts.append(f"[Image: {node[1]}]")
                    if node[2]:
                        text_parts.append(f"Caption: {node[2]}") 
                text_parts.append("")

            full_text = "\n".join(text_parts).strip()

            # Update UI
            preview_text = full_text[:1200] + ("..." if len(full_text) > 1200 else "")
            self.preview.delete("1.0", "end")
            self.preview.insert("1.0", preview_text)

            meta_info = (
                f"Title: {meta.get('title','')}\n"
                f"Author: {meta.get('author','')}\n"
                f"Date: {meta.get('date','')}\n"
                f"Tags: {meta.get('tags','')}\n"
                f"URL: {final}"
            )
            self.meta_box.delete("1.0", "end")
            self.meta_box.insert("1.0", meta_info)

            # Save TXT if requested
            if self.save_txt_var.get():
                if out_path.lower().endswith(".txt"):
                    txt_path = out_path
                else:
                    txt_path = os.path.splitext(out_path)[0] + ".txt"
                self.set_status("Saving TXT...")
                try:
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(full_text)
                    self.set_status(f"Saved TXT: {txt_path}")
                except Exception as e:
                    messagebox.showwarning("TXT save failed", f"Could not save TXT: {e}")
                    self.set_status("Failed to save TXT")

            # Save PDF if requested
            if self.save_pdf_var.get():
                pdf_path = out_path
                if not pdf_path.lower().endswith(".pdf"):
                    pdf_path = os.path.splitext(pdf_path)[0] + ".pdf"
                self.set_status("Composing PDF (downloading images)...")
                try:
                    create_pdf(pdf_path, meta, meta.get('nodes', []))
                    self.set_status(f"Saved PDF: {pdf_path}")
                    if pdf_path and os.path.exists(pdf_path):
                        open_file(pdf_path)
                except Exception as e:
                    messagebox.showwarning("PDF creation failed", f"Could not create PDF: {e}")
                    self.set_status("Failed to create PDF")

            messagebox.showinfo("Done", "Saved requested outputs.")
            self.set_status("Ready")

        except Exception as e:
            messagebox.showerror("Error", f"Failed: {e}")
            self.set_status("Error.")
        finally:
            try:
                self.scrape_btn.configure(state="normal")
            except Exception:
                try:
                    self.scrape_btn.config(state="normal")
                except Exception:
                    pass

    def run(self):
        self.root.mainloop()

def main():
    app = ScraperGUI()
    app.run()

if __name__ == "__main__":
    main()
