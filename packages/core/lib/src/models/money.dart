/// Money arrives from the API as a decimal **string** and is kept that way until it is displayed.
/// Parsing it into a double is how a 1,249.99 order becomes 1,249.9899999999998 on someone's phone.
class Money {
  const Money(this.amount);

  factory Money.parse(Object? value) {
    if (value == null) return const Money('0.00');
    return Money(value.toString());
  }

  final String amount;

  double get asDouble => double.tryParse(amount) ?? 0;

  /// "৳1,24,999.99" — Bangladeshi grouping (lakh/crore), done by hand so the app does not depend
  /// on locale data being initialised before the first price is ever drawn.
  String format({String symbol = '৳'}) {
    final negative = amount.startsWith('-');
    final parts = amount.replaceFirst('-', '').split('.');
    final whole = parts.first;
    final fraction = parts.length > 1 ? parts[1].padRight(2, '0').substring(0, 2) : '00';
    final buffer = StringBuffer();
    if (whole.length <= 3) {
      buffer.write(whole);
    } else {
      final head = whole.substring(0, whole.length - 3);
      final tail = whole.substring(whole.length - 3);
      final grouped = <String>[];
      var rest = head;
      while (rest.length > 2) {
        grouped.insert(0, rest.substring(rest.length - 2));
        rest = rest.substring(0, rest.length - 2);
      }
      if (rest.isNotEmpty) grouped.insert(0, rest);
      buffer
        ..write(grouped.join(','))
        ..write(',')
        ..write(tail);
    }
    return '${negative ? '-' : ''}$symbol$buffer.$fraction';
  }

  bool get isZero => asDouble == 0;

  @override
  String toString() => amount;
}
