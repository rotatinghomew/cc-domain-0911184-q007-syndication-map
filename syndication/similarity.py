"""Evidence used to compare source records across syndication partners.

Text alone is never enough: partner sites rewrite titles, while unrelated
notices can share the same template.  The comparison therefore reports the
individual signals it found and lets the association layer decide whether the
combination is strong enough to join two source records.
"""

import datetime as _dt
import re
import unicodedata

CANONICAL_FIELDS = (
    'canonical',
    'canonical_url',
    'original_url',
    'syndicated_from',
    'repost_of',
    'source_url',
)
BODY_FIELDS = ('body', 'content', 'text', 'summary', 'description', 'excerpt')
TITLE_FIELDS = ('title', 'headline')
ISSUER_FIELDS = ('issuer', 'author', 'organization', 'organisation', 'department')
DATE_FIELDS = ('published', 'published_at', 'date', 'issued', 'updated')

_URL_PATTERN = re.compile(r'^https?://', re.IGNORECASE)


def _first_text(item, fields):
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return ''


def body_text(item):
    """Return the published body, or an empty string when none was supplied."""
    return _first_text(item, BODY_FIELDS)


def title_text(item):
    return _first_text(item, TITLE_FIELDS)


def issuer_text(item):
    return _first_text(item, ISSUER_FIELDS)


def canonical_urls(item):
    urls = []
    for field in CANONICAL_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and _URL_PATTERN.match(value.strip()):
            urls.append(value.strip())
    return urls


def normalize(text):
    if not isinstance(text, str):
        text = str(text or '')
    text = unicodedata.normalize('NFKC', text).lower()
    return ''.join(character for character in text if character.isalnum())


def shingles(text, size=2):
    text = normalize(text)
    if not text:
        return set()
    if len(text) <= size:
        return {text}
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def jaccard(left, right):
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace('Z', '+00:00')
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def _dates_close(left, right):
    left_stamp = next((_timestamp(left.get(field)) for field in DATE_FIELDS
                       if _timestamp(left.get(field)) is not None), None)
    right_stamp = next((_timestamp(right.get(field)) for field in DATE_FIELDS
                        if _timestamp(right.get(field)) is not None), None)
    if left_stamp is None or right_stamp is None:
        return None
    return abs(left_stamp - right_stamp) <= 3 * 86400


def compare(left, right, left_urls=(), right_urls=(), canonical_left=(),
            canonical_right=(), shared_body=False):
    """Compare two source records and describe the evidence found.

    ``left_urls`` and ``right_urls`` are the address lineages of the two
    records.  A canonical reference is only usable when it resolves to exactly
    one of the other record's addresses; a shared channel/homepage address must
    never act as evidence, so callers pass pre-validated ``canonical_left`` and
    ``canonical_right`` keys for that case.
    """
    reasons = []
    left_url_set = set(left_urls)
    right_url_set = set(right_urls)

    for url in sorted(set(canonical_left)):
        reasons.append({'kind': 'canonical_reference', 'url': url,
                        'from': 'left', 'to': 'right'})
    for url in sorted(set(canonical_right)):
        reasons.append({'kind': 'canonical_reference', 'url': url,
                        'from': 'right', 'to': 'left'})

    body = jaccard(shingles(body_text(left)), shingles(body_text(right)))
    title = jaccard(shingles(title_text(left)), shingles(title_text(right)))
    left_issuer = normalize(issuer_text(left))
    right_issuer = normalize(issuer_text(right))
    issuer_match = bool(left_issuer and right_issuer and left_issuer == right_issuer)
    dates_close = _dates_close(left, right)

    signals = sum([title >= 0.5, issuer_match, bool(dates_close)])
    score = 0.7 * body + 0.15 * title
    score += 0.075 * (1 if issuer_match else 0)
    score += 0.075 * (1 if dates_close else 0)

    # A contradiction is stronger than any amount of shared wording: partner
    # sites use the same template, and unrelated notices may share a template
    # verbatim.  Differing issuers or far-apart dates therefore veto an
    # automatic join even when the body is byte-identical.
    left_issuer_present = bool(left_issuer)
    right_issuer_present = bool(right_issuer)
    issuer_conflict = left_issuer_present and right_issuer_present and not issuer_match
    dates_conflict = dates_close is False
    contradiction = issuer_conflict or dates_conflict
    # ``shared_body`` means the exact body is published by more than these two
    # records.  That is a template or boilerplate, not evidence of one notice.
    template = shared_body and not (issuer_match or dates_close is True)

    if body:
        reasons.append({
            'kind': 'content_similarity',
            'body': round(body, 4),
            'title': round(title, 4),
            'issuer_match': issuer_match,
            'dates_close': dates_close,
            'signals': signals,
            'score': round(score, 4),
        })
    if title >= 0.5 and not body:
        reasons.append({'kind': 'title_similarity', 'title': round(title, 4)})

    hard = bool(reasons and reasons[0]['kind'] == 'canonical_reference')
    if hard:
        decision = 'hard'
    elif contradiction or template:
        decision = 'proposed' if body >= 0.60 else 'none'
    elif body >= 0.95 and (issuer_match or dates_close is True):
        decision = 'strong'
    elif body >= 0.85 and (issuer_match or dates_close is True or title >= 0.5):
        decision = 'provisional'
    elif body >= 0.60 and (issuer_match or dates_close is True or title >= 0.5
                           or score >= 0.50):
        decision = 'proposed'
    elif title >= 0.95 and (issuer_match or dates_close is True):
        decision = 'proposed'
    else:
        decision = 'none'

    return {
        'decision': decision,
        'body_similarity': round(body, 4),
        'title_similarity': round(title, 4),
        'issuer_match': issuer_match,
        'dates_close': dates_close,
        'signals': signals,
        'score': round(score, 4),
        'shared_body': bool(shared_body),
        'contradiction': contradiction,
        'reasons': reasons,
    }
