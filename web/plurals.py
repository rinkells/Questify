"""Ukrainian plural forms, for the web's texts and its ``uk_plural`` filter."""


def uk_plural(number, forms):
    """The form for ``number``: ``uk_plural(3, 'день,дні,днів')`` is ``'дні'``."""
    one, few, many = forms.split(',')
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many
