"""Local display helpers; never mutate authoritative report evidence or findings.

Numbers are rounded only for presentation. Text remains plain text: renderers
must escape it themselves. No database, model, or additional facts are used.
"""

from decimal import Decimal, InvalidOperation, localcontext
import re

from chatbot.reports import BusinessReport, aggregate_result, decimal_value

_NUMBER = r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUMERIC = re.compile(rf"(?<![\w.,+\-]){_NUMBER}(?![\w,]|\.\d|[-/]\d)")
_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b\d{1,2}/\d{1,2}/\d{4}\b")
_BOILERPLATE = re.compile(
    r"^\s*(?:Evidence report\.\s*|"
    r"Findings below are locally verified aggregate values, not inferred business outcomes\.\s*)",
    re.I,
)


def display_number(value: object) -> str:
    """Group numeric values with commas and at most two decimals; retain nulls.

    Non-numeric labels are unchanged. Decimal arithmetic avoids float conversion
    and supports the same bounded numeric range as authoritative evidence.
    """
    if value is None:
        return "Missing"
    text = str(value)
    if not re.fullmatch(_NUMBER, text.strip()):
        return text
    number = decimal_value(text.strip().replace(",", ""))
    if number is None:
        return text
    with localcontext() as context:
        context.prec = 220
        try:
            rounded = number.quantize(Decimal("0.01"))
        except InvalidOperation:
            return text
    if not rounded:
        rounded = abs(rounded)
    return format(rounded, ",.2f").rstrip("0").rstrip(".")


def human_label(value: str) -> str:
    """Turn a column identifier into a readable heading without inventing units."""
    return value.replace("_", " ").strip().capitalize()


def clean_report_text(text: str, report: BusinessReport, *, keep_references: bool = False) -> str:
    """Clean plain display text, removing exact evidence citations by default.

    Set keep_references=True for stable one-based references such as [1].
    Unknown bracketed text is preserved; internal IDs are not pattern-guessed.
    Dates, words containing digits, and friendly citation numbers are not rounded.
    """
    references = {f"[{item.id}]": f"[{index}]" if keep_references else ""
                  for index, item in enumerate(report.evidence, 1)}
    if references:
        pattern = "|".join(re.escape(key) for key in references)
        text = re.sub(pattern, lambda match: references[match.group()], text)
    text = text.strip()
    # The standard partial-report notice is substantive and must survive cleanup.
    partial = "Partial report: some requested evidence is unavailable."
    prefix = ""
    if text.startswith(partial):
        prefix, text = partial + " ", text[len(partial):].lstrip()
    while _BOILERPLATE.match(text):
        text = _BOILERPLATE.sub("", text, count=1)
    text = prefix + text
    text = text.replace("_", " ")
    protected = [match.span() for match in _DATE.finditer(text)]
    protected.extend(match.span() for match in re.finditer(r"\[[^\]]*\]", text))
    protected.extend(match.span(1) for match in re.finditer(
        r"\b(?:from|to|in|year|period)\s+((?:19|20|21)\d{2})\b", text, re.I))

    def number(match: re.Match[str]) -> str:
        if any(start <= match.start() < end for start, end in protected):
            return match.group()
        return display_number(match.group())

    text = _NUMERIC.sub(number, text)
    return re.sub(r"[ \t]+", " ", text).strip()


def key_findings(report: BusinessReport) -> tuple[str, ...]:
    """Select up to six existing observations: comparisons, scalars, then groups.

    Never calculate new totals or replace missing values. Selection is separate
    from the original findings contract, including its original row order.
    """
    summary = clean_report_text(report.summary, report)
    # Split at verified observation starts, not every full stop: labels such as
    # H.R. and decimal values are not sentence boundaries.
    comparisons = [sentence.strip() for sentence in re.split(r"(?=Among returned groups,|[A-Z][^.]* changed by )", summary)
                   if re.search(r"\b(?:highest|lowest|changed by|compared|difference)\b", sentence, re.I)]
    scalar_ids = {item.id for item in report.evidence
                  if (result := aggregate_result(item)) is not None and len(result.rows) == 1}
    scalars, groups = [], []
    for finding in report.findings:
        target = scalars if any(f"[{item_id}]" in finding for item_id in scalar_ids) else groups
        target.append(clean_report_text(finding, report))
    selected: list[str] = []
    for finding in comparisons + scalars + groups:
        if finding and finding not in selected:
            selected.append(finding)
        if len(selected) == 6:
            break
    return tuple(selected)


def executive_summary(report: BusinessReport) -> str:
    """Present the supplied narrative, or a short honest aggregate overview."""
    summary = clean_report_text(report.summary, report)
    partial = "Partial report: some requested evidence is unavailable."
    if summary and summary != partial:
        return summary
    observations = key_findings(report)[:3]
    overview = ("This report summarizes the available aggregate results. " + " ".join(observations)
                if observations else "No aggregate-safe numeric findings are available; missing values are not zero.")
    return (partial + " " if summary == partial else "") + overview


def evidence_heading(report: BusinessReport, index: int) -> str:
    """Use a business question only for evidence allowed by the privacy gate."""
    item = report.evidence[index - 1]
    if aggregate_result(item) is not None:
        question = clean_report_text(item.question, report)
        if question:
            return question
    return f"Section {index}"