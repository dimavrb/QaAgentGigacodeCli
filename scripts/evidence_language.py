"""Check Russian narrative; exact defect quotations require explicit exceptions."""
import re
import unicodedata


def text_errors(text):
    errors = []
    for word in re.findall(r'[^\W_]+', text, flags=re.UNICODE):
        scripts = set()
        for letter in word:
            name = unicodedata.name(letter, '')
            if 'CYRILLIC' in name: scripts.add('cyrillic')
            elif 'LATIN' in name: scripts.add('latin')
            elif unicodedata.category(letter).startswith('L'): scripts.add('foreign')
        if 'foreign' in scripts:
            errors.append('foreign writing system: ' + word)
        if {'latin', 'cyrillic'} <= scripts:
            errors.append('mixed Cyrillic/Latin word: ' + word)
    return errors


def check_review_language(review):
    errors = []
    for case in review.get('cases', []):
        for check in case.get('checks', []):
            prefix = case['case_id'] + '/' + check['check_id']
            for name in ('note', 'reason'):
                value = check.get(name)
                if value is not None and not isinstance(value, str):
                    errors.append(prefix + ': ' + name + ' must be a string')
            # Quote validity is checked first by the caller. The exception applies
            # only to the literal evidence, never to an invented narrative note.
            exception = check.get('literal_defect_quote', False)
            if type(exception) is not bool:
                errors.append(prefix + ': literal_defect_quote must be boolean')
    def visit(value, pointer=''):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'evidence' and 'check_id' in value and value.get('literal_defect_quote') is True:
                    continue
                visit(item, pointer + '/' + key)
        elif isinstance(value, list):
            for index, item in enumerate(value): visit(item, pointer + '/' + str(index))
        elif isinstance(value, str):
            errors.extend(pointer + ': ' + error for error in text_errors(value))
    visit(review)
    return errors
