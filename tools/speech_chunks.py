"""Split reference speech at word/clause boundaries without dropping text."""
import re

UNFINISHED_ENDINGS = {'a', 'an', 'the', 'to', 'of', 'for', 'with', 'from', 'into',
                     'and', 'or', 'but', 'because', 'that', 'which', 'while',
                     'my', 'your', 'his', 'her', 'their', 'our', 'worth', 'over',
                     'under', 'around', 'about', 'between', 'at', 'than'}


def word_units(token):
    number = token.strip('.,;!?\"\'()')
    if re.fullmatch(r'[0-9][0-9,.:]*(?:[–—-][0-9][0-9,.:]*)?\+?', number):
        # Written numerals expand in speech. A nominal 12-word sentence with
        # 25,000 repeated in the fixed-reference soak; its two clauses did not.
        # Ranges expand both endpoints; treating 20,000–30,000 as one ordinary
        # word bypassed the same numeric boundary used for individual amounts.
        return sum(4 if digits >= 4 else 2 if digits >= 3 else 1
                   for digits in (sum(c.isdigit() for c in part)
                                  for part in re.split(r'[–—-]',number.rstrip('+'))))
    return 1


def split_repeated_clauses(piece):
    # Repeated comma-separated clauses induced an extra spoken repetition in
    # the fixed-reference soak. Keep each clause, including intentional repeats,
    # but generate them separately instead of feeding the repeated tail at once.
    clauses = []
    current = []
    for token in piece.split():
        current.append(token)
        if re.search(r'[.!?;,]["\')]*$', token):
            clauses.append(' '.join(current))
            current = []
    if current:
        clauses.append(' '.join(current))
    normalized = [re.findall(r"\w+(?:'\w+)?", clause.lower()) for clause in clauses]
    repeated = any(len(a) >= 3 and a == b for a, b in zip(normalized, normalized[1:]))
    return clauses if repeated else [piece]


def split_speech_text(text, language='en', words=12):
    if language == 'zh':
        # Preserve every character; prefer punctuation in the latter half.
        result = []
        while len(text) > 24:
            cuts = [i+1 for i, c in enumerate(text[:24]) if c in '，。！？；' and i >= 11]
            cut = cuts[-1] if cuts else 24
            result.append(text[:cut])
            text = text[cut:]
        if text:
            result.append(text)
        return result
    tokens = text.split()
    result = []
    while sum(word_units(token) for token in tokens) > words:
        window = []
        units = 0
        for token in tokens:
            if window and units+word_units(token) > words:
                break
            window.append(token)
            units += word_units(token)
        sentences = [i+1 for i, word in enumerate(window)
                     if i >= 3 and re.search(r'[.!?][\"\')]*$', word)]
        has_clause_cut = any(i >= 3 and re.search(r'[.!?;,][\"\')]*$',token)
                             for i, token in enumerate(window))
        if not sentences and not has_clause_cut:
            # Finish a nearby ordinary-word sentence instead of separating a
            # noun phrase such as "crocodile tears" at word 12. Reuse the
            # existing 15-unit short-tail allowance; expanded numerals retain
            # their stricter boundary because the fixed numeric control repeated.
            units = 0
            ordinary = True
            for i, token in enumerate(tokens):
                units += word_units(token)
                ordinary = ordinary and word_units(token) == 1
                if units > words+3 or not ordinary:
                    break
                if i >= len(window) and re.search(r'[.!?][\"\')]*$',token):
                    sentences = [i+1]
                    break
        cuts = sentences or [i+1 for i, word in enumerate(window)
                if (i >= 3 or (i >= 2 and any(word_units(t)>1 for t in window[:i+1])))
                and re.search(r'[.!?;,][\"\')]*$', word)]
        cut = cuts[-1] if cuts else len(window)
        if not cuts:
            clause_starts = [i for i,token in enumerate(window)
                            if i >= 4 and len(window)-i >= 3 and token.lower() in ('because','but')]
            if clause_starts:
                cut = clause_starts[-1]
            # A fragment such as "paid money to" provoked repeating generation
            # in the real replay. Keep the preposition/determiner with its phrase.
            while cut > 4 and tokens[cut-1].lower() in UNFINISHED_ENDINGS:
                cut -= 1
        result.append(' '.join(tokens[:cut]))
        tokens = tokens[cut:]
    if tokens:
        tail = ' '.join(tokens)
        if len(tokens) < 4 and result and sum(word_units(t) for t in result[-1].split()+tokens) <= words+3:
            result[-1] += ' '+tail
        else:
            result.append(tail)
    return [clause for piece in result for clause in split_repeated_clauses(piece)]


def estimated_audio_seconds(text, language='en'):
    # Calibrated conservatively from the saved Qiqi/PocketTTS run (~.344 s/word).
    return max(.8, len(text)*.18 if language == 'zh' else len(text.split())*.36)
