"""Detect conflicting explicit amounts without choosing a source transcript.

SenseVoice's ITN switch is a model input, not merely display formatting. Check
the same audio twice only for utterances containing explicit amounts/years.
The second stream reuses the loaded recognizer; it loads no additional model.
Agreement does not establish that either reading matches the speaker.
"""
import re
from translation_checks import required_number_counts


def significant_quantities(text):
    return {value: count for value, count in required_number_counts(text).items()
            if float(value) >= 100}


def numeric_audit(formatted, literal):
    """Return evidence and reasons; never rewrite either recognized text."""
    issues = []
    # 一一万 may be a stutter or a digit sequence. Repeated unit constructions
    # such as 千五千/七千五千 may be clipped speech or adjacent amounts. The
    # existing translation quantity parser must not resolve these ambiguities.
    spans = re.findall(r'[零〇一二两三四五六七八九十百千万亿]+', literal)
    ambiguous = [span for span in spans if
                 re.search(r'([零〇一二两三四五六七八九])\1[百千万亿]', span)
                 or any(span.count(unit) > 1 for unit in '十百千万亿')]
    if ambiguous:
        issues.append('ambiguous_literal_quantity')
    formatted_counts = significant_quantities(formatted)
    literal_counts = significant_quantities(literal)
    if formatted_counts != literal_counts:
        issues.append('itn_quantity_disagreement')
    return {'formatted_text': formatted, 'literal_text': literal,
            'formatted_quantities': formatted_counts,
            'literal_quantities': literal_counts,
            'ambiguous_literal_spans': ambiguous, 'issues': issues,
            'same_audio': True, 'human_source_transcript_verified': False}


def polarity_audit(online, refined):
    """Flag the measured 可能/不可能 disagreement; do not choose a reading.

    The online feature-frame endpoint and received-audio cut can differ. This
    is deliberately an uncertainty signal, not two aligned human transcripts.
    Other negation, roles and terms are outside this narrow comparison.
    """
    def counts(text):
        # Do not assign qualified doubt (不太可能) to either strict cue.
        text = re.sub(r'不(?:太|大|很)可能', '', text)
        cues = re.findall(r'不可能|可能', text)
        return {'possible': cues.count('可能'), 'impossible': cues.count('不可能')}
    before, after = counts(online), counts(refined)
    if not any(before.values()) or not any(after.values()):
        return None
    return {'online_text': online, 'refined_text': refined,
            'online_cues': before, 'refined_cues': after,
            'issues': ['modal_polarity_disagreement'] if before != after else [],
            'transcripts_exactly_aligned': False,
            'human_source_transcript_verified': False}


def boundary_fragment_audit(text):
    """Flag the two reproduced unresolved boundary forms without completing them.

    More context recovered a following verb/subject in fixed-audio controls.
    Until bounded context assembly is validated, a guessed complete English
    clause must not be spoken. This is uncertainty, not a replacement reading.
    """
    issues = []
    if re.search(r'不管自己[^。！？!?；;]{0,4}在[。！？!?]*$', text):
        issues.append('unfinished_self_clause_at_boundary')
    if re.search(r'(?:妈妈|母亲)[^。！？!?；;]{0,20}不相信了(?:妈妈|母亲)[。！？!?]*$', text):
        issues.append('ambiguous_repeated_mother_at_boundary')
    if not issues:
        return None
    return {'source_text':text, 'issues':issues, 'replacement_transcript':None,
            'context_resolution_verified':False, 'human_source_transcript_verified':False}


def refine_with_numeric_check(recognizer, pcm, online_text=''):
    stream = recognizer.create_stream()
    stream.set_option('use_itn', '1')
    stream.accept_waveform(16000, pcm)
    recognizer.decode_stream(stream)
    text = stream.result.text.strip()
    if not significant_quantities(text) and not significant_quantities(online_text):
        return text, None
    try:
        literal_stream = recognizer.create_stream()
        literal_stream.set_option('use_itn', '0')
        literal_stream.accept_waveform(16000, pcm)
        recognizer.decode_stream(literal_stream)
        literal = literal_stream.result.text.strip()
        if not literal:
            raise RuntimeError('Literal numeric check returned no text')
        audit = numeric_audit(text, literal)
    except Exception as error:
        audit = {'formatted_text': text, 'literal_text': '',
                 'issues': ['numeric_check_failed'], 'error': str(error),
                 'same_audio': True, 'human_source_transcript_verified': False}
    return text, audit
