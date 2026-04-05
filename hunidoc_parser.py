import io
import re
import pdfplumber
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# =========================================================
# SEGÉDFÜGGVÉNYEK
# =========================================================

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def inline_text(text: str) -> str:
    text = clean_text(text)
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def remove_accents(text: str) -> str:
    mapping = str.maketrans(
        "áéíóöőúüűÁÉÍÓÖŐÚÜŰ",
        "aeiooouuuAEIOOOUUU"
    )
    return text.translate(mapping)


def extract_all_text_from_pdf(pdf_bytes: bytes):
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            pages.append(clean_text(txt))
    return pages


# =========================================================
# PÉNZ KEZELÉS
# =========================================================

def parse_huf_amount_or_none(value: str):
    """
    Kezeli ezeket is:
    - 80 970,00 Ft
    - 55 500.00 Ft
    - 55500 HUF
    - 0,00 Ft
    """
    if value is None:
        return None

    s = value.strip()
    if not s:
        return None

    s = s.replace("\xa0", " ")
    s = s.replace("Ft", "").replace("ft", "")
    s = s.replace("HUF", "").replace("huf", "")
    s = s.strip()

    # magyar vagy angol tizedes formátum
    # pl: 80 970,00 | 55 500.00 | 55500
    m = re.search(r"(\d[\d\s]*)(?:[.,](\d{2}))?", s)
    if not m:
        return None

    integer_part = (m.group(1) or "").replace(" ", "")
    decimal_part = m.group(2)

    if not integer_part.isdigit():
        return None

    if decimal_part is not None:
        try:
            return int(round(float(f"{integer_part}.{decimal_part}")))
        except Exception:
            return None

    return int(integer_part)


def parse_huf_amount(value: str) -> int:
    result = parse_huf_amount_or_none(value)
    return result if result is not None else 0


# =========================================================
# KERESÉS
# =========================================================

def find_first_line_value(patterns, text):
    """
    Csak az adott sor végéig olvas.
    Nem csúszik át a következő mezőre.
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = match.group(1)
            return value.strip() if value else ""
    return ""


def find_first(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            value = match.group(1)
            if value is not None:
                value = value.strip()
                if value != "":
                    return value
    return ""


# =========================================================
# NORMALIZÁLÁS
# =========================================================

def normalize_bejelentes_modja(value: str) -> str:
    if not value:
        return ""
    v = remove_accents(value.lower())

    if "telefon" in v:
        return "telefon"
    if "web" in v:
        return "web"
    if "email" in v:
        return "email"

    return ""


def normalize_ugyfel_tipus(value: str) -> str:
    if not value:
        return ""
    v = remove_accents(value.lower())

    if "igen" in v:
        return "uj"
    if "nem" in v:
        return "meglevo"

    return ""


def normalize_fizetesi_mod(value: str) -> str:
    if not value:
        return ""
    v = remove_accents(value.lower())

    if "kp" in v or "keszpenz" in v:
        return "kp"
    if "kartya" in v:
        return "kartya"
    if "utalas" in v:
        return "utalas"

    return ""


# =========================================================
# MEZŐK KINYERÉSE
# =========================================================

def extract_munkalap_id(text):
    return find_first([
        r"\b(MSZ-\d+)\b"
    ], text)


def extract_datum(text):
    """
    PDF-ben pl. 2026.03.31.
    Visszaadjuk ISO formában: 2026-03-31
    """
    matches = re.findall(r"\b(20\d{2})[.\-](\d{2})[.\-](\d{2})\.?\b", text)
    if matches:
        y, m, d = matches[-1]
        return f"{y}-{m}-{d}"
    return ""


def extract_bejelentes_modja(text):
    raw = find_first_line_value([
        r"Bejelentés módja:[^\S\r\n]*([^\r\n]*)",
        r"Bejelentes modja:[^\S\r\n]*([^\r\n]*)",
    ], text)
    return normalize_bejelentes_modja(raw)


def extract_ugyfel_tipus(text):
    raw = find_first_line_value([
        r"Új megrendelő:[^\S\r\n]*([^\r\n]*)",
        r"Uj megrendelo:[^\S\r\n]*([^\r\n]*)",
    ], text)
    return normalize_ugyfel_tipus(raw)


def extract_munkadij(text):
    raw = find_first_line_value([
        r"Munkadíj összesen:[^\S\r\n]*([^\r\n]*)",
        r"Munkadij osszesen:[^\S\r\n]*([^\r\n]*)",
    ], text)
    val = parse_huf_amount_or_none(raw)
    return val if val is not None else 0


def extract_anyagkoltseg(text):
    raw = find_first_line_value([
        r"Anyagköltség összesen:[^\S\r\n]*([^\r\n]*)",
        r"Anyagkoltseg osszesen:[^\S\r\n]*([^\r\n]*)",
    ], text)
    val = parse_huf_amount_or_none(raw)
    return val if val is not None else 0


def extract_vegosszeg(text, inline):
    raw = find_first_line_value([
        r"Bruttó összeg:[^\S\r\n]*([^\r\n]*)",
        r"Brutto osszeg:[^\S\r\n]*([^\r\n]*)",
    ], text)

    val = parse_huf_amount_or_none(raw)
    if val is not None:
        return val

    # óvatos fallback: csak a Bruttó összeg környezetében keres
    match = re.search(
        r"Bruttó összeg\s*:\s*([0-9][0-9\s.,]*\s*(?:Ft|HUF))",
        inline,
        re.IGNORECASE
    ) or re.search(
        r"Brutto osszeg\s*:\s*([0-9][0-9\s.,]*\s*(?:Ft|HUF))",
        inline,
        re.IGNORECASE
    )

    if match:
        val = parse_huf_amount_or_none(match.group(1))
        if val is not None:
            return val

    return 0


def extract_fizetesi_mod(text):
    raw = find_first([
        r"Fizetési mód\s*:\s*([^\n]+)",
        r"Fizetesi mod\s*:\s*([^\n]+)",
    ], text)
    return normalize_fizetesi_mod(raw)


def extract_munkavegzo(text, inline):
    """
    Stabilabb logika:
    - a Tevékenységek blokkot nem csak Munkadíj-ig, hanem a következő nagyobb szekcióig vágjuk
    - a blokkban HUF/Ft utáni utolsó két szavas névformát keressük
    """
    block = find_first([
        r"Tevékenységek\s*:\s*(.*?)(?:Munkadíj összesen|Munkadij osszesen|Felhasznált eszközök|Felhasznalt eszkozok|Anyagköltség összesen|Anyagkoltseg osszesen)",
        r"Tevekenysegek\s*:\s*(.*?)(?:Munkadíj összesen|Munkadij osszesen|Felhasznált eszközök|Felhasznalt eszkozok|Anyagköltség összesen|Anyagkoltseg osszesen)",
    ], text)

    if block:
        # minden névszerű találat a blokkban
        names = re.findall(
            r"(?:Ft|HUF)\s+([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+(?:\s+[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+){1,2})",
            block
        )
        if names:
            return names[-1].strip()

        # fallback: bármely két-három szavas név a blokkban
        names = re.findall(
            r"\b([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+(?:\s+[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+){1,2})\b",
            block
        )
        blacklist = {
            "Bejelentés Módja",
            "Uj Megrendelo",
            "Bruttó Összeg",
            "Brutto Osszeg",
            "Fizetési Mód",
            "Fizetesi Mod",
            "Felhasznált Eszközök",
            "Anyagköltség Összesen",
            "Alkatrészre Vár",
            "Munkadíj Összesen",
        }
        names = [n for n in names if n not in blacklist]
        if names:
            return names[-1].strip()

    # végső fallback: teljes dokumentumban HUF/Ft utáni név
    names = re.findall(
        r"(?:Ft|HUF)\s+([A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+(?:\s+[A-ZÁÉÍÓÖŐÚÜŰ][a-záéíóöőúüű]+){1,2})",
        inline
    )
    if names:
        return names[-1].strip()

    return ""


# =========================================================
# EREDMÉNY ÖSSZEÁLLÍTÁS
# =========================================================

def build_result(pages):
    text = "\n".join(pages)
    inline = inline_text(text)

    munkalap_id = extract_munkalap_id(text)
    datum = extract_datum(text)
    bejelentes_modja = extract_bejelentes_modja(text)
    ugyfel_tipus = extract_ugyfel_tipus(text)
    munkavegzo = extract_munkavegzo(text, inline)
    munkadij = extract_munkadij(text)
    anyagkoltseg = extract_anyagkoltseg(text)
    vegosszeg = extract_vegosszeg(text, inline)
    fizetesi_mod = extract_fizetesi_mod(text)

    profit = vegosszeg - anyagkoltseg

    return {
        "munkalap_id": munkalap_id,
        "datum": datum,
        "bejelentes_modja": bejelentes_modja,
        "ugyfel_tipus": ugyfel_tipus,
        "munkavegzo": munkavegzo,
        "munkadij": munkadij,
        "anyagkoltseg": anyagkoltseg,
        "vegosszeg": vegosszeg,
        "profit": profit,
        "fizetesi_mod": fizetesi_mod,
        "statusz": "feldolgozva",
        "darab": 1
    }


# =========================================================
# UPLOAD OLDAL
# =========================================================

UPLOAD_PAGE = """
<!DOCTYPE html>
<html lang="hu">
<head>
    <meta charset="UTF-8">
    <title>Hunidoc PDF parser teszt</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 960px;
            margin: 40px auto;
            padding: 20px;
            background: #f7f7f7;
        }
        .box {
            background: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.08);
        }
        h1 { margin-top: 0; }
        input[type="file"] { margin: 16px 0; }
        button {
            background: #2563eb;
            color: white;
            border: none;
            padding: 12px 18px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 16px;
        }
        button:hover { background: #1d4ed8; }
        pre {
            background: #111827;
            color: #e5e7eb;
            padding: 16px;
            border-radius: 10px;
            overflow-x: auto;
            white-space: pre-wrap;
        }
        .note {
            color: #555;
            margin-bottom: 20px;
        }
    </style>
</head>
<body>
    <div class="box">
        <h1>Hunidoc PDF parser teszt</h1>
        <p class="note">Válassz ki egy PDF munkalapot, majd kattints a Feldolgozás gombra.</p>

        <form id="uploadForm">
            <input type="file" id="fileInput" name="file" accept=".pdf" required>
            <br>
            <button type="submit">Feldolgozás</button>
        </form>

        <h2>Eredmény</h2>
        <pre id="result">Még nincs eredmény.</pre>
    </div>

    <script>
        const form = document.getElementById("uploadForm");
        const resultBox = document.getElementById("result");

        form.addEventListener("submit", async function(e) {
            e.preventDefault();

            const fileInput = document.getElementById("fileInput");
            if (!fileInput.files.length) {
                resultBox.textContent = "Kérlek válassz ki egy PDF fájlt.";
                return;
            }

            const formData = new FormData();
            formData.append("file", fileInput.files[0]);

            resultBox.textContent = "Feldolgozás folyamatban...";

            try {
                const response = await fetch("/parse", {
                    method: "POST",
                    body: formData
                });

                const data = await response.json();
                resultBox.textContent = JSON.stringify(data, null, 2);
            } catch (error) {
                resultBox.textContent = "Hiba történt: " + error;
            }
        });
    </script>
</body>
</html>
"""


# =========================================================
# API
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "Hunidoc PDF parser",
        "status": "ok",
        "upload_page": "/upload",
        "parse_endpoint": "/parse"
    })


@app.route("/upload", methods=["GET"])
def upload_page():
    return render_template_string(UPLOAD_PAGE)


@app.route("/parse", methods=["POST"])
def parse_pdf():
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "error": "no file"}), 400

    pdf_bytes = file.read()

    try:
        pages = extract_all_text_from_pdf(pdf_bytes)
        data = build_result(pages)

        return jsonify({
            "success": True,
            "data": data,
            "debug": {
                "page_count": len(pages),
                "text_preview": "\n".join(pages)[:2500]
            }
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(port=5000, debug=True)