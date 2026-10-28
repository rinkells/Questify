import codecs
import math

from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser, get_encoding
from rest_framework.utils import json


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        # json.loads turns 1e999 into inf; int(inf) in a primary-key lookup
        # then raises OverflowError, which no field catches (a 500).
        raise ValueError(f'Number out of range: {text}')
    return value


class SafeJSONParser(JSONParser):
    """JSON parser that answers malformed edge cases with a 400.

    The stdlib decoder raises RecursionError for deeply nested arrays, which
    DRF's parser does not catch, and accepts overflowing numbers such as
    1e999 as infinity; both would otherwise reach the client as a 500.
    """

    def parse(self, stream, media_type=None, parser_context=None):
        # As JSONParser.parse, plus parse_float; get_encoding keeps its
        # refusal of bytes-to-bytes codecs such as bz2_codec.
        encoding = get_encoding(parser_context or {})
        try:
            decoded_stream = codecs.getreader(encoding)(stream)
            parse_constant = json.strict_constant if self.strict else None
            return json.load(
                decoded_stream,
                parse_constant=parse_constant,
                parse_float=_finite_float,
            )
        except RecursionError as exc:
            raise ParseError('JSON document is nested too deeply.') from exc
        except ValueError as exc:
            raise ParseError(f'JSON parse error - {exc}') from exc
