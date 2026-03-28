"""
LightGBM-based diagnosis conclusion engine.

Design
------
- Each dataset intent (question) produces 2 numerical features:
    1. Normalised response-list index  (0 = not answered, 1 = last option)
    2. Positivity score                (1.0 = clearly symptomatic, 0.0 = normal)
- Synthetic training data is generated directly from the dataset's response
  options so no labelled clinical records are required.
- A LightGBM multiclass model is trained per area-of-concern and saved to
  backend/models/ as a pickle bundle.
- At inference time the bundle is loaded (trained once and cached on disk) and
  the patient's actual Q/A answers are vectorised and classified.
"""

import json
import os
import pickle
import re
import logging

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

try:
    import lightgbm as lgb
    LIGHTGBM_IMPORT_ERROR = None
except Exception as exc:
    lgb = None
    LIGHTGBM_IMPORT_ERROR = exc

logger = logging.getLogger(__name__)

# ── paths ──────────────────────────────────────────────────────────────────────
# This file lives at  backend/api/lgbm_diagnosis.py
# Datasets are at     backend/Datasets/
# Saved models go to  backend/models/
_BASE = os.path.dirname(os.path.dirname(__file__))   # backend/
DATASETS_DIR = os.path.join(_BASE, 'Datasets')
MODELS_DIR   = os.path.join(_BASE, 'models')

MAX_SCAN_FILES = 6
MAX_FILE_TEXT_CHARS = 4000
MAX_PDF_PAGES = 2

AREA_MAPPING = {
    'Head':           'Head.json',
    'Breast':         'Breasts.json',
    'Breasts':        'Breasts.json',
    'Pelvis':         'Pelvic.json',
    'Urinary System': 'UrinarySystem.json',
    'Skin':           'Skin.json',
    'Hormonal':       'Hormone.json',
}

AREA_ALIASES = {
    'head': 'Head',
    'breast': 'Breast',
    'breasts': 'Breast',
    'pelvis': 'Pelvis',
    'urinary system': 'Urinary System',
    'skin': 'Skin',
    'hormonal': 'Hormonal',
}

AREA_DIAGNOSIS_CANDIDATES = {
    'Head': [
        {'name': 'Migraine', 'keywords': ['migraine', 'headache', 'aura', 'photophobia', 'nausea', 'unilateral', 'throbbing']},
        {'name': 'Tension-Type Headache', 'keywords': ['tension', 'stress', 'tight', 'band', 'neck', 'posture']},
        {'name': 'Hormonal Headache', 'keywords': ['menstrual', 'cycle', 'period', 'hormonal', 'contraceptive', 'menopause']},
    ],
    'Pelvis': [
        {'name': 'Endometriosis', 'keywords': ['endometriosis', 'pelvic', 'pain', 'period', 'dysmenorrhea', 'intercourse', 'bloating']},
        {'name': 'PCOS', 'keywords': ['pcos', 'irregular', 'period', 'ovary', 'acne', 'hirsutism', 'weight']},
        {'name': 'Pelvic Inflammatory Disease', 'keywords': ['pid', 'pelvic', 'discharge', 'sti', 'fever', 'lower', 'abdominal']},
        {'name': 'Uterine Fibroid Related Symptoms', 'keywords': ['fibroid', 'heavy', 'bleeding', 'pressure', 'pelvic', 'distension']},
    ],
    'Breast': [
        {'name': 'Fibrocystic Breast Changes', 'keywords': ['fibrocystic', 'cyclical', 'lump', 'tenderness', 'bilateral', 'period']},
        {'name': 'Mastalgia', 'keywords': ['mastalgia', 'breast', 'pain', 'tenderness', 'cyclical']},
        {'name': 'Mastitis', 'keywords': ['mastitis', 'breastfeeding', 'redness', 'warmth', 'fever', 'pain']},
        {'name': 'Suspicious Breast Malignancy', 'keywords': ['malignancy', 'cancer', 'hard', 'lump', 'nipple', 'discharge', 'family', 'history']},
    ],
    'Breasts': [
        {'name': 'Fibrocystic Breast Changes', 'keywords': ['fibrocystic', 'cyclical', 'lump', 'tenderness', 'bilateral', 'period']},
        {'name': 'Mastalgia', 'keywords': ['mastalgia', 'breast', 'pain', 'tenderness', 'cyclical']},
        {'name': 'Mastitis', 'keywords': ['mastitis', 'breastfeeding', 'redness', 'warmth', 'fever', 'pain']},
        {'name': 'Suspicious Breast Malignancy', 'keywords': ['malignancy', 'cancer', 'hard', 'lump', 'nipple', 'discharge', 'family', 'history']},
    ],
    'Urinary System': [
        {'name': 'Urinary Tract Infection', 'keywords': ['uti', 'urinary', 'burning', 'frequency', 'urgency', 'infection']},
        {'name': 'Interstitial Cystitis', 'keywords': ['cystitis', 'bladder', 'pain', 'pelvic', 'urgency', 'chronic']},
        {'name': 'Stress Urinary Incontinence', 'keywords': ['stress', 'incontinence', 'leakage', 'cough', 'sneeze', 'exercise']},
    ],
    'Skin': [
        {'name': 'Hormonal Acne', 'keywords': ['acne', 'hormonal', 'jawline', 'breakout', 'cycle']},
        {'name': 'Rosacea', 'keywords': ['rosacea', 'facial', 'redness', 'flushing', 'sensitivity']},
        {'name': 'Dermatitis/Allergic Skin Reaction', 'keywords': ['rash', 'itching', 'allergy', 'cosmetics', 'irritation']},
    ],
    'Hormonal': [
        {'name': 'Thyroid Dysfunction Pattern', 'keywords': ['thyroid', 'fatigue', 'weight', 'cold', 'heat', 'hair']},
        {'name': 'PCOS Hormonal Pattern', 'keywords': ['pcos', 'irregular', 'period', 'hirsutism', 'acne']},
        {'name': 'Perimenopausal Hormonal Changes', 'keywords': ['menopause', 'night', 'sweats', 'hot', 'flash', 'irregular']},
    ],
}

# keywords used to score response positivity
_POSITIVE_KW = [
    'yes', 'present', 'positive', 'severe', 'worse', 'increased',
    'frequent', 'pain', 'consistently', 'significantly', 'notable',
    'first trimester', 'second', 'third', 'both',
]
_NEGATIVE_KW = [
    'no', 'none', 'not', 'never', 'normal', 'absent', 'not applicable',
    'not using', 'not sure',
]


def _tokenise(text: str) -> list:
    return [t for t in re.split(r'[^a-z0-9]+', (text or '').lower()) if t]


def _normalise_area(area: str) -> str:
    key = (area or '').strip().lower()
    return AREA_ALIASES.get(key, (area or '').strip())


def _condition_keywords(conditions: list) -> dict:
    keyword_map = {}
    for condition in conditions:
        base_tokens = _tokenise(condition)
        aliases = set(base_tokens)

        # Common clinical alias examples.
        if 'migraine' in aliases:
            aliases.update(['headache', 'aura', 'photophobia', 'nausea'])
        if 'endometriosis' in aliases:
            aliases.update(['pelvic', 'menstrual', 'cycle', 'cramp', 'dysmenorrhea'])
        if 'uti' in aliases or ('urinary' in aliases and 'infection' in aliases):
            aliases.update(['burning', 'urination', 'frequency', 'urgency'])

        keyword_map[condition] = aliases
    return keyword_map


def _extract_file_text_signal(file_path: str) -> str:
    """
    Read text signal from supported uploads.
    Supported: txt, pdf, docx, images (OCR when optional deps exist).
    """
    if not file_path or not os.path.exists(file_path):
        return ''
    lower = file_path.lower()
    try:
        if lower.endswith('.txt'):
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read(MAX_FILE_TEXT_CHARS)

        if lower.endswith('.pdf'):
            try:
                from pypdf import PdfReader
                reader = PdfReader(file_path)
                parts = []
                for page in reader.pages[:MAX_PDF_PAGES]:
                    parts.append(page.extract_text() or '')
                return ' '.join(parts)[:MAX_FILE_TEXT_CHARS]
            except Exception:
                return ''

        if lower.endswith('.docx'):
            try:
                from docx import Document
                doc = Document(file_path)
                text = ' '.join((p.text or '') for p in doc.paragraphs)
                return text[:MAX_FILE_TEXT_CHARS]
            except Exception:
                return ''

        if lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tif', '.tiff', '.heic', '.heif')):
            # OCR is expensive; keep it opt-in via environment flag.
            if os.environ.get('ENABLE_OCR', '0') != '1':
                return ''
            try:
                import pytesseract
                from PIL import Image
                with Image.open(file_path) as img:
                    return (pytesseract.image_to_string(img) or '')[:MAX_FILE_TEXT_CHARS]
            except Exception:
                return ''

        return ''
    except Exception:
        return ''


def _keyword_condition_scores(qa_pairs: list, conditions: list, scan_files: list | None) -> np.ndarray:
    """
    Secondary ML signal (heuristic/keyword model) blended with LightGBM.
    Uses answer text + file names + extracted file text.
    """
    scores = np.zeros(len(conditions), dtype=np.float32)
    keyword_map = _condition_keywords(conditions)

    texts = []
    for qa in qa_pairs:
        if isinstance(qa, dict):
            texts.append(str(qa.get('question', '')))
            texts.append(str(qa.get('answer', '')))

    for sf in (scan_files or [])[:MAX_SCAN_FILES]:
        if isinstance(sf, dict):
            texts.append(str(sf.get('name', '')))
            texts.append(_extract_file_text_signal(str(sf.get('path', ''))))
        else:
            texts.append(str(sf))

    joined_tokens = set(_tokenise(' '.join(texts)))
    if not joined_tokens:
        return scores

    for idx, condition in enumerate(conditions):
        kws = keyword_map.get(condition, set())
        overlap = len(joined_tokens.intersection(kws))
        if overlap:
            scores[idx] = float(overlap)

    total = float(scores.sum())
    if total > 0:
        scores = scores / total
    return scores


def _tfidf_condition_scores(qa_pairs: list, conditions: list, scan_files: list | None) -> np.ndarray:
    """Third signal: TF-IDF + cosine similarity between patient text and condition descriptors."""
    patient_parts = []

    for qa in qa_pairs:
        if isinstance(qa, dict):
            patient_parts.append(str(qa.get('question', '')))
            patient_parts.append(str(qa.get('answer', '')))

    for sf in (scan_files or [])[:MAX_SCAN_FILES]:
        if isinstance(sf, dict):
            patient_parts.append(str(sf.get('name', '')))
            patient_parts.append(_extract_file_text_signal(str(sf.get('path', ''))))
        else:
            patient_parts.append(str(sf))

    patient_text = ' '.join(patient_parts).strip()
    if not patient_text:
        return np.zeros(len(conditions), dtype=np.float32)

    keyword_map = _condition_keywords(conditions)
    condition_docs = [
        f"{condition.replace('_', ' ')} {' '.join(sorted(keyword_map.get(condition, set())))}"
        for condition in conditions
    ]

    corpus = [patient_text] + condition_docs
    try:
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), max_features=2500)
        matrix = vectorizer.fit_transform(corpus)
        sims = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
    except Exception:
        return np.zeros(len(conditions), dtype=np.float32)

    sims = np.array(sims, dtype=np.float32)
    total = float(sims.sum())
    if total > 0:
        sims = sims / total
    return sims


def _collect_patient_text(qa_pairs: list, scan_files: list | None) -> str:
    parts = []
    for qa in qa_pairs:
        if isinstance(qa, dict):
            parts.append(str(qa.get('question', '')))
            parts.append(str(qa.get('answer', '')))
    for sf in (scan_files or [])[:MAX_SCAN_FILES]:
        if isinstance(sf, dict):
            parts.append(str(sf.get('name', '')))
            parts.append(_extract_file_text_signal(str(sf.get('path', ''))))
        else:
            parts.append(str(sf))
    return ' '.join(parts).strip()


def _candidate_diagnosis_scores(area: str, patient_text: str) -> list:
    """Area-specific concrete diagnosis clues from patient text evidence."""
    candidates = AREA_DIAGNOSIS_CANDIDATES.get(area, [])
    if not candidates:
        return []

    text_tokens = set(_tokenise(patient_text))
    keyword_scores = np.zeros(len(candidates), dtype=np.float32)
    reason_tokens = []

    for i, item in enumerate(candidates):
        kws = {k.lower() for k in item.get('keywords', [])}
        overlap = sorted(text_tokens.intersection(kws))
        keyword_scores[i] = float(len(overlap))
        reason_tokens.append(overlap)

    docs = [patient_text]
    docs.extend([
        f"{item['name']} {' '.join(item.get('keywords', []))}"
        for item in candidates
    ])
    try:
        vec = TfidfVectorizer(ngram_range=(1, 2), max_features=2000).fit_transform(docs)
        tfidf_scores = cosine_similarity(vec[0:1], vec[1:]).flatten().astype(np.float32)
    except Exception:
        tfidf_scores = np.zeros(len(candidates), dtype=np.float32)

    if keyword_scores.sum() > 0:
        keyword_scores = keyword_scores / float(keyword_scores.sum())
    if tfidf_scores.sum() > 0:
        tfidf_scores = tfidf_scores / float(tfidf_scores.sum())

    final_scores = (0.6 * keyword_scores) + (0.4 * tfidf_scores)
    if final_scores.sum() <= 0:
        final_scores = np.ones(len(candidates), dtype=np.float32) / max(len(candidates), 1)
    else:
        final_scores = final_scores / float(final_scores.sum())

    ranked = np.argsort(final_scores)[::-1]
    results = []
    for idx in ranked[:3]:
        results.append({
            'condition': candidates[idx]['name'],
            'confidence': round(float(final_scores[idx]) * 100, 1),
            'matched_keywords': reason_tokens[idx][:6],
        })
    return results


# ── helpers ────────────────────────────────────────────────────────────────────

def _load_dataset(area: str) -> dict:
    canonical_area = _normalise_area(area)
    filename = AREA_MAPPING.get(canonical_area)
    if not filename:
        raise ValueError(f"Unknown area of concern: '{area}'")
    path = os.path.join(DATASETS_DIR, filename)
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _condition_from_tag(tag: str) -> str:
    """'migraine_menstrual_cycle_link' → 'migraine'"""
    parts = [p for p in tag.split('_') if len(p) > 2]
    return parts[0] if parts else tag


def _pretty_condition_name(raw_condition: str, area: str) -> str:
    """Convert internal condition token to user-facing diagnosis label."""
    if not raw_condition:
        return f'{area} Related Condition'

    stop_tokens = {
        'you', 'do', 'have', 'are', 'what', 'when', 'how', 'why', 'which',
        'did', 'does', 'can', 'could', 'would', 'should', 'is', 'was', 'were',
        'at', 'of', 'for', 'to', 'from', 'in', 'on', 'and', 'or', 'any',
    }
    raw = raw_condition.replace('_', ' ').strip()
    tokens = [t for t in _tokenise(raw) if t not in stop_tokens]

    # Promote known clinical diagnoses when present in token stream.
    if 'endometriosis' in tokens:
        return 'Endometriosis'
    if 'migraine' in tokens:
        return 'Migraine'
    if 'pcos' in tokens:
        return 'PCOS'
    if 'cystitis' in tokens:
        return 'Interstitial Cystitis'
    if 'uti' in tokens or ('urinary' in tokens and 'infection' in tokens):
        return 'Urinary Tract Infection'

    if not tokens:
        return f'{area} Related Condition'

    # One-token generic terms are too vague for diagnosis labels.
    generic_terms = {
        'pain', 'history', 'change', 'changes', 'frequency', 'regular',
        'sleep', 'diet', 'doctor', 'screening', 'symptom', 'symptoms',
    }
    if len(tokens) == 1 and tokens[0] in generic_terms:
        return f'{area} Related Condition'

    return ' '.join(tokens).title()


def _positivity_score(answer: str, allowed: list) -> float:
    """
    Returns a [0, 1] symptomatic-intensity score.
    1.0 = clearly positive / symptomatic
    0.0 = clearly negative / normal
    0.5 = neutral or not answered
    """
    a = (answer or '').strip().lower()
    if not a:
        return 0.5
    for kw in _POSITIVE_KW:
        if kw in a:
            return 1.0
    for kw in _NEGATIVE_KW:
        if kw in a:
            return 0.0
    # fall back to position in allowed list (earlier = more positive)
    try:
        idx = [r.strip() for r in allowed].index(answer.strip())
        return round(1.0 - idx / max(len(allowed) - 1, 1), 3)
    except ValueError:
        return 0.5


def _encode_response(answer: str, allowed: list) -> float:
    """Normalised position [0, 1] of the answer in the allowed list.  0 if missing."""
    a = (answer or '').strip()
    if not a or not allowed:
        return 0.0
    try:
        idx = [r.strip() for r in allowed].index(a)
        return (idx + 1) / len(allowed)
    except ValueError:
        return 0.0


def _summarize_scan_files(scan_files: list | None) -> dict:
    files = scan_files or []
    total = len(files)
    image_count = 0
    pdf_count = 0
    doc_count = 0
    txt_count = 0

    for item in files:
        name = (item.get('name') if isinstance(item, dict) else str(item) or '').lower()
        if name.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.bmp')):
            image_count += 1
        elif name.endswith('.pdf'):
            pdf_count += 1
        elif name.endswith(('.doc', '.docx')):
            doc_count += 1
        elif name.endswith('.txt'):
            txt_count += 1

    return {
        'total': total,
        'image_count': image_count,
        'pdf_count': pdf_count,
        'doc_count': doc_count,
        'txt_count': txt_count,
    }


def _vectorise(qa_pairs: list, intents: list, scan_summary: dict | None = None) -> np.ndarray:
    """
    Convert an ordered Q/A list into a flat feature vector.
    Feature layout:  [enc_0, pos_0,  enc_1, pos_1,  …]  (2 × n_intents values)
    """
    feats = np.zeros(len(intents) * 2 + 5, dtype=np.float32)
    for i, intent in enumerate(intents):
        allowed = intent.get('responses', [])
        answer = ''
        if i < len(qa_pairs):
            item = qa_pairs[i]
            answer = item.get('answer') or '' if isinstance(item, dict) else str(item)
        feats[i * 2]     = _encode_response(answer, allowed)
        feats[i * 2 + 1] = _positivity_score(answer, allowed)
    scan = scan_summary or {
        'total': 0,
        'image_count': 0,
        'pdf_count': 0,
        'doc_count': 0,
        'txt_count': 0,
    }
    base = len(intents) * 2
    # Normalised scan features (cap at 5 for stability)
    feats[base] = min(scan.get('total', 0), 5) / 5.0
    feats[base + 1] = min(scan.get('image_count', 0), 5) / 5.0
    feats[base + 2] = min(scan.get('pdf_count', 0), 5) / 5.0
    feats[base + 3] = min(scan.get('doc_count', 0), 5) / 5.0
    feats[base + 4] = min(scan.get('txt_count', 0), 5) / 5.0

    return feats


# ── condition grouping ─────────────────────────────────────────────────────────

def _conditions_list(intents: list) -> list:
    seen, result = set(), []
    for intent in intents:
        c = _condition_from_tag(intent['tag'])
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


# ── synthetic training data ────────────────────────────────────────────────────

def _generate_synthetic_data(intents: list, conditions: list,
                              n_samples: int = 3000):
    """
    For each synthetic patient:
      - Pick one condition as the 'primary diagnosis'
      - Questions related to that condition → sample positive (early-index) responses
      - Unrelated questions → sample negative (late-index) responses
      - 20 % chance of random noise on any question to improve generalisation
    """
    rng = np.random.default_rng(seed=42)

    condition_for_intent = []
    for intent in intents:
        c = _condition_from_tag(intent['tag'])
        condition_for_intent.append(conditions.index(c) if c in conditions else 0)

    X, y = [], []
    for _ in range(n_samples):
        primary = int(rng.integers(0, len(conditions)))
        row = []
        for i, intent in enumerate(intents):
            allowed = intent.get('responses', [])
            if not allowed:
                row.extend([0.0, 0.5])
                continue

            n = len(allowed)
            related = (condition_for_intent[i] == primary)

            if rng.random() < 0.2:                        # noise
                idx = int(rng.integers(0, n))
            elif related:                                  # positive signal
                idx = int(rng.integers(0, min(2, n)))
            else:                                          # negative signal
                idx = int(rng.integers(max(0, n - 2), n))

            answer = allowed[idx]
            row.append((idx + 1) / n)
            row.append(_positivity_score(answer, allowed))

        # Add synthetic scan evidence. Cases with stronger symptom patterns
        # are more likely to include uploaded clinical files.
        if rng.random() < 0.7:
            total_scans = int(rng.integers(1, 4))
        else:
            total_scans = int(rng.integers(0, 2))

        image_count = int(rng.integers(0, total_scans + 1)) if total_scans else 0
        remaining = max(0, total_scans - image_count)
        pdf_count = int(rng.integers(0, remaining + 1)) if remaining else 0
        remaining -= pdf_count
        doc_count = int(rng.integers(0, remaining + 1)) if remaining else 0
        remaining -= doc_count
        txt_count = remaining

        row.extend([
            min(total_scans, 5) / 5.0,
            min(image_count, 5) / 5.0,
            min(pdf_count, 5) / 5.0,
            min(doc_count, 5) / 5.0,
            min(txt_count, 5) / 5.0,
        ])

        X.append(row)
        y.append(primary)

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int32)


# ── model persistence ──────────────────────────────────────────────────────────

def _model_path(area: str) -> str:
    canonical_area = _normalise_area(area)
    safe = re.sub(r'[^A-Za-z0-9]', '_', canonical_area)
    return os.path.join(MODELS_DIR, f'lgbm_{safe}.pkl')


def _train_and_save(area: str) -> dict:
    if lgb is None:
        raise RuntimeError(f'LightGBM is unavailable: {LIGHTGBM_IMPORT_ERROR}')

    canonical_area = _normalise_area(area)
    dataset   = _load_dataset(canonical_area)
    intents   = dataset.get('ourIntents', [])
    if not intents:
        raise ValueError(f"Dataset for '{canonical_area}' contains no intents.")

    conditions = _conditions_list(intents)
    n_classes  = len(conditions)

    X, y = _generate_synthetic_data(intents, conditions)

    params = dict(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        max_depth=6,
        min_child_samples=10,
        colsample_bytree=0.8,
        subsample=0.8,
        random_state=42,
        verbose=-1,
        n_jobs=-1,
        class_weight='balanced',
    )
    if n_classes <= 2:
        params['objective'] = 'binary'
        model = lgb.LGBMClassifier(**params)
    else:
        params['objective'] = 'multiclass'
        params['num_class'] = n_classes
        model = lgb.LGBMClassifier(**params)

    model.fit(X, y)

    os.makedirs(MODELS_DIR, exist_ok=True)
    bundle = {
        'model':      model,
        'conditions': conditions,
        'intents':    intents,
        'area':       area,
        'n_classes':  n_classes,
        'feature_version': 2,
        'input_dim': int(X.shape[1]),
    }
    with open(_model_path(area), 'wb') as f:
        pickle.dump(bundle, f)

    return bundle


def _load_model(area: str) -> dict:
    path = _model_path(area)
    if os.path.exists(path):
        try:
            with open(path, 'rb') as f:
                bundle = pickle.load(f)
                intents = bundle.get('intents', [])
                expected_dim = len(intents) * 2 + 5
                if bundle.get('feature_version') != 2:
                    return _train_and_save(area)
                if bundle.get('input_dim') != expected_dim:
                    return _train_and_save(area)
                return bundle
        except Exception:
            pass          # corrupt file – retrain
    return _train_and_save(area)


# ── public API ─────────────────────────────────────────────────────────────────

def run_lgbm_diagnosis(qa_pairs: list, area_of_concern: str, scan_files: list | None = None) -> dict:
    """
    Run LightGBM inference for a patient.

    Parameters
    ----------
    qa_pairs : list of dicts  {'index': int, 'question': str, 'answer': str}
               ordered by question index (same order as dataset intents).
    area_of_concern : str matching AREA_MAPPING keys.

    Returns
    -------
    dict with:
        top_conditions  – list of {condition, confidence} ordered by probability
        positive_count  – number of clearly symptomatic answers
        negative_count  – number of clearly negative answers
        total_answered  – questions with a non-empty answer
        total_questions – total questions in the dataset
        diagnosis_text  – human-readable conclusion string
    """
    canonical_area = _normalise_area(area_of_concern)
    dataset = _load_dataset(canonical_area)
    intents = dataset.get('ourIntents', [])
    if not intents:
        raise ValueError(f"Dataset for '{canonical_area}' contains no intents.")

    conditions = _conditions_list(intents)
    n_classes = len(conditions)
    if n_classes == 0:
        raise ValueError(f"Dataset for '{canonical_area}' contains no diagnosable conditions.")

    scan_summary = _summarize_scan_files(scan_files)
    proba = np.zeros(n_classes, dtype=np.float32)

    if lgb is not None:
        bundle = _load_model(canonical_area)
        model = bundle['model']
        vec = _vectorise(qa_pairs, intents, scan_summary).reshape(1, -1)
        proba = np.array(model.predict_proba(vec)[0], dtype=np.float32)
    else:
        logger.warning('LightGBM unavailable, using fallback diagnosis signals only: %s', LIGHTGBM_IMPORT_ERROR)

    keyword_proba = _keyword_condition_scores(qa_pairs, conditions, scan_files)
    tfidf_proba = _tfidf_condition_scores(qa_pairs, conditions, scan_files)

    # Hybrid ensemble. If LightGBM is unavailable, rely on text/fallback signals.
    if np.any(proba):
        proba = (
            (0.70 * np.array(proba, dtype=np.float32))
            + (0.20 * np.array(keyword_proba, dtype=np.float32))
            + (0.10 * np.array(tfidf_proba, dtype=np.float32))
        )
    else:
        proba = (
            (0.70 * np.array(keyword_proba, dtype=np.float32))
            + (0.30 * np.array(tfidf_proba, dtype=np.float32))
        )

    if not np.any(proba):
        proba = np.ones(n_classes, dtype=np.float32) / float(n_classes)

    norm = float(proba.sum())
    if norm > 0:
        proba = proba / norm

    # For binary LightGBM predict_proba returns shape (1, 2); for multiclass (1, n).
    top_n        = min(3, n_classes)
    top_indices  = np.argsort(proba)[::-1][:top_n]
    lgbm_conditions = [
        {
            'condition':  _pretty_condition_name(conditions[i], canonical_area),
            'confidence': round(float(proba[i]) * 100, 1),
        }
        for i in top_indices
    ]

    patient_text = _collect_patient_text(qa_pairs, scan_files)
    concrete_conditions = _candidate_diagnosis_scores(canonical_area, patient_text)
    top_conditions = concrete_conditions if concrete_conditions else lgbm_conditions

    primary_condition = top_conditions[0]['condition'] if top_conditions else 'Inconclusive'

    # categorise actual patient answers
    pos_qa = [
        qa for qa in qa_pairs
        if _positivity_score(
            qa.get('answer', '') if isinstance(qa, dict) else '', []
        ) >= 0.8
    ]
    neg_qa = [
        qa for qa in qa_pairs
        if _positivity_score(
            qa.get('answer', '') if isinstance(qa, dict) else '', []
        ) <= 0.2
    ]
    answered = [
        qa for qa in qa_pairs
        if (qa.get('answer') if isinstance(qa, dict) else qa)
    ]

    positive_clues = [
        {
            'index': qa.get('index'),
            'question': qa.get('question', ''),
            'answer': qa.get('answer', ''),
        }
        for qa in pos_qa[:5]
        if isinstance(qa, dict)
    ]

    matched_keywords = concrete_conditions[0].get('matched_keywords', []) if concrete_conditions else []

    return {
        'primary_clue': primary_condition,
        'secondary_clue': top_conditions[1]['condition'] if len(top_conditions) > 1 else None,
        'patient_suggestion': (
            f"Based on your answers and uploaded files, you may have {primary_condition}."
        ),
        'why_this_clue': matched_keywords,
        'top_conditions':   top_conditions,
        'positive_count':   len(pos_qa),
        'negative_count':   len(neg_qa),
        'total_answered':   len(answered),
        'total_questions':  len(qa_pairs),
        'positive_clues':   positive_clues,
        'scan_summary':     scan_summary,
        'diagnosis_text':   _build_text(
            top_conditions, len(pos_qa), canonical_area,
            len(answered), len(qa_pairs),
            positive_clues,
            scan_summary,
            scan_files or [],
            matched_keywords,
        ),
    }


def _build_text(top_conditions: list, positive_count: int,
                area: str, answered: int, total: int,
                positive_clues: list, scan_summary: dict,
                scan_files: list,
                why_this_clue: list | None = None) -> str:
    lines = [f'Diagnostic Clue ({area})', '']

    if not top_conditions:
        lines.append(
            'Not enough information is available to prepare a draft conclusion. '
            'Please review the responses and complete any missing clinical '
            'information before finalising.'
        )
        return '\n'.join(lines)

    primary = top_conditions[0]
    differential_text = ''
    if len(top_conditions) > 1:
        differential_text = ', '.join(
            [item['condition'] for item in top_conditions[1:3]]
        )

    lines.append(f"Likely diagnosis clue: {primary['condition']}")
    lines.append(
        f"Suggested patient diagnosis: You may have {primary['condition']}."
    )
    if why_this_clue:
        lines.append(f"Why this clue: matched evidence -> {', '.join(why_this_clue)}")
    if differential_text:
        lines.append(f"Secondary clue(s): {differential_text}")
    return '\n'.join(lines)


# ── batch training utility ─────────────────────────────────────────────────────

def train_all_areas() -> None:
    """Pre-train and save LightGBM models for every dataset area."""
    for area in AREA_MAPPING:
        print(f'Training LightGBM for: {area} ...', end=' ', flush=True)
        try:
            _train_and_save(area)
            print('✓')
        except Exception as exc:
            print(f'✗  ({exc})')
