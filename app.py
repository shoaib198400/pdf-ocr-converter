import io
import os
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pikepdf
from pikepdf import Name, PdfImage
from PIL import Image
import streamlit as st

# ── Tesseract paths (Windows local vs Linux cloud) ───────────────────────────
if platform.system() == "Windows":
    TESSERACT_DIR = r"C:\Users\31931190\AppData\Local\Programs\Tesseract-OCR"
    TESSDATA_DIR  = rf"{TESSERACT_DIR}\tessdata"
    IS_CLOUD      = False
else:
    TESSERACT_DIR = None
    TESSDATA_DIR  = None
    IS_CLOUD      = True

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PDF OCR Converter",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main-header {font-size:2.2rem; font-weight:700; margin-bottom:0.2rem;}
    .sub-header  {color:#666; margin-bottom:1.5rem;}
    .stat-box    {background:#f0f4ff; border-radius:8px; padding:12px 16px;
                  text-align:center; border:1px solid #d0d8ff;}
    .stat-label  {font-size:0.75rem; color:#666; text-transform:uppercase;
                  letter-spacing:0.05em;}
    .stat-value  {font-size:1.4rem; font-weight:700; color:#1a1a2e;}
    .error-tag   {background:#f8d7da; color:#721c24; padding:2px 8px;
                  border-radius:12px; font-size:0.8rem;}
    .step-label  {font-size:0.82rem; color:#444; margin-top:2px;}
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_ocr_env() -> dict:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if TESSERACT_DIR:
        env["PATH"] = TESSERACT_DIR + os.pathsep + env.get("PATH", "")
    if TESSDATA_DIR:
        env["TESSDATA_PREFIX"] = TESSDATA_DIR
    return env


def get_page_count(pdf_bytes: bytes) -> int:
    try:
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def run_ocr_with_progress(
    input_path: str, output_path: str,
    language: str, deskew: bool, clean: bool,
    total_pages: int,
    prog_bar, step_text,
    prog_start: float = 0.02, prog_end: float = 0.72,
) -> tuple[bool, str]:
    """
    Run ocrmypdf via Popen and stream stderr to update the progress bar
    in real time. OCR phase covers prog_start → prog_end of the bar.
    """
    cmd = [
        sys.executable, "-m", "ocrmypdf",
        "--output-type", "pdf",
        "--force-ocr",
        "-O", "0",
        "-l", language,
        "--jobs", "2",
    ]
    if deskew:
        cmd.append("--deskew")
    if clean:
        cmd.append("--clean")
    cmd += [input_path, output_path]

    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        text=True,
        env=get_ocr_env(),
    )

    pages_seen = 0
    all_stderr = []
    ocr_span   = prog_end - prog_start

    for raw_line in iter(proc.stderr.readline, ""):
        line = raw_line.strip()
        if not line:
            continue
        all_stderr.append(line)

        # "    33 page already has text!" or "    33 Weighted average..."
        m = re.match(r"^\s*(\d+)\s+(?:page|Weighted)", line)
        if m:
            pages_seen = max(pages_seen, int(m.group(1)))
            if total_pages:
                pct = prog_start + (pages_seen / total_pages) * ocr_span * 0.80
                prog_bar.progress(min(pct, prog_end * 0.82))
            step_text.markdown(
                f'<div class="step-label">🔍 OCR — page {pages_seen}'
                f'{"/" + str(total_pages) if total_pages else ""}</div>',
                unsafe_allow_html=True,
            )
        elif re.search(r"Parsing \d+ pages? with HocrParser", line):
            prog_bar.progress(prog_start + ocr_span * 0.85)
            step_text.markdown(
                '<div class="step-label">📝 Building text layer...</div>',
                unsafe_allow_html=True,
            )
        elif "Postprocessing" in line:
            prog_bar.progress(prog_start + ocr_span * 0.95)
            step_text.markdown(
                '<div class="step-label">🔧 Postprocessing...</div>',
                unsafe_allow_html=True,
            )

    proc.stderr.close()
    proc.wait()

    ok  = proc.returncode == 0
    msg = "\n".join(all_stderr[-30:])   # keep last 30 lines for error display
    return ok, msg


def compress_pdf_with_progress(
    input_bytes: bytes, jpeg_quality: int, max_dim: int,
    total_pages: int,
    prog_bar, step_text,
    prog_start: float = 0.72, prog_end: float = 0.97,
) -> tuple[bytes, int, int]:
    """
    Re-compress PDF images with per-page progress updates.
    Returns (compressed_bytes, images_compressed, bytes_saved).
    """
    pdf         = pikepdf.open(io.BytesIO(input_bytes))
    compressed  = 0
    saved_total = 0
    comp_span   = prog_end - prog_start

    for page_idx, page in enumerate(pdf.pages, 1):
        # Update progress for each page
        pct = prog_start + (page_idx / max(total_pages, 1)) * comp_span
        prog_bar.progress(min(pct, prog_end))
        step_text.markdown(
            f'<div class="step-label">🗜️ Compressing — page {page_idx}'
            f'{"/" + str(total_pages) if total_pages else ""}</div>',
            unsafe_allow_html=True,
        )

        for _, xobj in page.images.items():
            if xobj.get("/Subtype") != Name("/Image"):
                continue
            w = int(xobj.get("/Width",  0))
            h = int(xobj.get("/Height", 0))
            if w * h < 10_000:
                continue
            filters = xobj.get("/Filter")
            if filters is not None:
                flist = ([str(f) for f in filters]
                         if isinstance(filters, pikepdf.Array)
                         else [str(filters)])
                if any(f in {"/JBIG2Decode", "/CCITTFaxDecode", "/JPXDecode"}
                       for f in flist):
                    continue
            try:
                pil_img = PdfImage(xobj).as_pil_image()
            except Exception:
                continue

            orig_len = len(xobj.read_raw_bytes())
            ow, oh   = pil_img.size
            if max(ow, oh) > max_dim:
                scale   = max_dim / max(ow, oh)
                pil_img = pil_img.resize(
                    (int(ow * scale), int(oh * scale)), Image.LANCZOS)

            if pil_img.mode in ("RGBA", "P"):
                pil_img = pil_img.convert("RGB")
            elif pil_img.mode not in ("RGB", "L"):
                try:
                    pil_img = pil_img.convert("RGB")
                except Exception:
                    continue

            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            new_bytes = buf.getvalue()
            if len(new_bytes) >= orig_len:
                continue

            xobj.write(new_bytes, filter=Name("/DCTDecode"))
            if pil_img.size != (ow, oh):
                xobj["/Width"]  = pil_img.width
                xobj["/Height"] = pil_img.height
            compressed  += 1
            saved_total += orig_len - len(new_bytes)

    step_text.markdown(
        '<div class="step-label">💾 Saving final PDF...</div>',
        unsafe_allow_html=True,
    )
    out_buf = io.BytesIO()
    pdf.save(out_buf, compress_streams=True,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)
    pdf.close()
    return out_buf.getvalue(), compressed, saved_total


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ── Session state init ────────────────────────────────────────────────────────
if "results" not in st.session_state:
    st.session_state.results = {}


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Settings")

    language = st.selectbox(
        "OCR Language",
        ["eng", "eng+osd"],
        help="Language pack for Tesseract",
    )
    jpeg_quality = st.slider(
        "Output JPEG Quality", 60, 95, 82,
        help="Higher = better quality, larger file size",
    )
    max_dim = st.select_slider(
        "Max image dimension (px)",
        options=[1000, 1500, 2000, 2500, 3000, 4000],
        value=2000,
        help="Images larger than this are downsampled",
    )
    deskew = st.toggle("Auto-deskew pages", value=False,
                       help="Straighten slightly tilted scans (slower)")
    clean  = st.toggle("Clean pages before OCR", value=False,
                       help="Remove noise before OCR (slower)")

    st.markdown("---")
    st.markdown("**Tesseract v5** · OCRmyPDF · pikepdf")
    if IS_CLOUD:
        st.caption("Processing runs on the server. Files are not stored.")
    else:
        st.caption("All processing runs locally on your machine.")


# ── Header ────────────────────────────────────────────────────────────────────
logo_path = Path(__file__).parent / "HP and DH.jpg"
col_logo, col_title = st.columns([3, 7])
with col_logo:
    if logo_path.exists():
        st.image(str(logo_path), width=300)
with col_title:
    st.markdown('<div style="height:18px"></div>', unsafe_allow_html=True)
    st.markdown('<div class="main-header">PDF OCR Converter</div>',
                unsafe_allow_html=True)
    st.markdown('<div class="sub-header">Convert scanned PDFs into fully searchable documents</div>',
                unsafe_allow_html=True)
st.markdown("---")


# ── Upload ────────────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Drop PDF files here or click to browse",
    type=["pdf"],
    accept_multiple_files=True,
    label_visibility="collapsed",
)

if uploaded:
    new_names = {f.name for f in uploaded}
    st.session_state.results = {
        k: v for k, v in st.session_state.results.items() if k in new_names
    }

    st.markdown(f"**{len(uploaded)} file(s) selected**")
    col_btn, col_clear, _ = st.columns([2, 2, 6])
    run_all   = col_btn.button("Convert All", type="primary",
                               use_container_width=True)
    clear_btn = col_clear.button("Clear Results", use_container_width=True)
    if clear_btn:
        st.session_state.results = {}
        st.rerun()

    if run_all:
        for idx, f in enumerate(uploaded):
            name      = f.name
            raw_bytes = f.read()
            orig_size = len(raw_bytes)

            # ── Per-file progress container ───────────────────────────────
            with st.container(border=True):
                st.markdown(f"**{idx+1} / {len(uploaded)} — {name}**")
                prog_bar  = st.progress(0.0)
                step_text = st.empty()

                step_text.markdown(
                    '<div class="step-label">📂 Analysing file...</div>',
                    unsafe_allow_html=True,
                )
                total_pages = get_page_count(raw_bytes)
                prog_bar.progress(0.02)

                with tempfile.TemporaryDirectory() as tmpdir:
                    src = Path(tmpdir) / name
                    ocr = Path(tmpdir) / f"ocr_{name}"
                    src.write_bytes(raw_bytes)

                    # Step 1 — OCR (2% → 72%)
                    step_text.markdown(
                        f'<div class="step-label">🔍 Starting OCR'
                        f'{f" ({total_pages} pages)" if total_pages else ""}...</div>',
                        unsafe_allow_html=True,
                    )
                    ok, msg = run_ocr_with_progress(
                        str(src), str(ocr), language, deskew, clean,
                        total_pages, prog_bar, step_text,
                    )

                    if not ok:
                        prog_bar.progress(1.0)
                        step_text.error(f"OCR failed — {msg[-200:]}")
                        st.session_state.results[name] = {
                            "status": "error", "error": msg, "orig_size": orig_size,
                        }
                        continue

                    ocr_bytes = ocr.read_bytes()

                    # Step 2 — Compress (72% → 97%)
                    try:
                        comp_bytes, n_imgs, saved = compress_pdf_with_progress(
                            ocr_bytes, jpeg_quality, max_dim,
                            total_pages, prog_bar, step_text,
                        )
                        final_size = len(comp_bytes)
                    except Exception:
                        comp_bytes = ocr_bytes
                        final_size = len(ocr_bytes)
                        n_imgs = saved = 0

                # Step 3 — Done (100%)
                prog_bar.progress(1.0)
                reduction = 100 * (1 - final_size / orig_size) if orig_size else 0
                step_text.success(
                    f"Done!  {human_size(orig_size)} → {human_size(final_size)}"
                    f"  ({reduction:.0f}% smaller)"
                )

                stem     = Path(name).stem
                out_name = f"{stem} (Searchable).pdf"
                st.session_state.results[name] = {
                    "status":     "done",
                    "out_name":   out_name,
                    "orig_size":  orig_size,
                    "final_size": final_size,
                    "reduction":  reduction,
                    "n_imgs":     n_imgs,
                    "data":       comp_bytes,
                }

        st.rerun()


# ── Results ───────────────────────────────────────────────────────────────────
results = st.session_state.results
done    = [r for r in results.values() if r["status"] == "done"]
errors  = [r for r in results.values() if r["status"] == "error"]

if results:
    st.markdown("---")
    st.markdown("### Results")

    if done:
        total_in   = sum(r["orig_size"]  for r in done)
        total_out  = sum(r["final_size"] for r in done)
        avg_reduce = 100 * (1 - total_out / total_in) if total_in else 0

        c1, c2, c3, c4 = st.columns(4)
        for col, label, val in [
            (c1, "Files Converted",   f"{len(done)}"),
            (c2, "Total Input Size",  human_size(total_in)),
            (c3, "Total Output Size", human_size(total_out)),
            (c4, "Avg Size Reduction", f"{avg_reduce:.0f}%"),
        ]:
            col.markdown(
                f'<div class="stat-box">'
                f'<div class="stat-label">{label}</div>'
                f'<div class="stat-value">{val}</div></div>',
                unsafe_allow_html=True,
            )

        st.markdown("")

        if len(done) > 1:
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for r in done:
                    zf.writestr(r["out_name"], r["data"])
            st.download_button(
                "Download All as ZIP",
                data=zip_buf.getvalue(),
                file_name="Searchable PDFs.zip",
                mime="application/zip",
                type="primary",
            )
            st.markdown("")

    for fname, r in results.items():
        with st.container():
            if r["status"] == "done":
                c_name, c_orig, c_out, c_red, c_dl = st.columns([4, 2, 2, 1.5, 2])
                c_name.markdown(f"**{r['out_name']}**")
                c_orig.markdown(f"In: `{human_size(r['orig_size'])}`")
                c_out.markdown( f"Out: `{human_size(r['final_size'])}`")
                c_red.markdown( f"**-{r['reduction']:.0f}%**")
                c_dl.download_button(
                    "Download",
                    data=r["data"],
                    file_name=r["out_name"],
                    mime="application/pdf",
                    key=f"dl_{fname}",
                )
            else:
                c1, c2 = st.columns([6, 4])
                c1.markdown(f"**{fname}**")
                c2.markdown(
                    f'<span class="error-tag">Failed</span> '
                    f'<small>{r.get("error","")[:120]}</small>',
                    unsafe_allow_html=True,
                )
            st.divider()

    if errors:
        with st.expander(f"{len(errors)} file(s) failed"):
            for r in errors:
                st.error(r.get("error", "Unknown error"))


# ── Empty state ───────────────────────────────────────────────────────────────
if not uploaded and not results:
    st.markdown("""
    <div style="text-align:center; padding:60px 20px; color:#888;">
        <div style="font-size:4rem;">📂</div>
        <div style="font-size:1.1rem; margin-top:12px;">
            Upload one or more scanned PDF files above to get started
        </div>
        <div style="font-size:0.85rem; margin-top:8px;">
            Supports scanned pages, mixed content, tables, and images
        </div>
    </div>
    """, unsafe_allow_html=True)
