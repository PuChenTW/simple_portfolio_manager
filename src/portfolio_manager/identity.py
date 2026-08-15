"""Instrument identity: stable IDs, issuer mapping, and classification with provenance.

Two rules shape this module:

* A classification value never overwrites another. Each (instrument, field, provenance) is stored
  separately and `resolve_classification` picks the winner by rank, so retracting a manual
  override restores the provider's original view intact.
* Nothing is inferred from ticker spelling. An unresolvable field stays `unclassified` and is
  reported in `warnings` rather than being filled with a plausible-looking guess.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError, not_found
from .models import Instrument, InstrumentAlias, InstrumentClassification, Issuer
from .schemas import (
    ClassificationFieldRead,
    InstrumentAliasRead,
    InstrumentProfileRead,
    IssuerRead,
    utc_now,
)
from .taxonomy import (
    CLASSIFICATION_FIELDS,
    PROVENANCE_RANK,
    AssetClass,
    Provenance,
    SecurityType,
    derive_from_provider,
)

YAHOO_PROVIDER = "yahoo"


@dataclass(frozen=True)
class ResolvedField:
    """A classification field's winning value plus the provenance that produced it."""

    field: str
    value: str | None
    provenance: Provenance
    source: str
    effective_at: datetime | None
    confidence: Decimal | None
    note: str | None


def _classification_rows(
    session: Session, instrument_id: str
) -> list[InstrumentClassification]:
    return list(
        session.scalars(
            select(InstrumentClassification).where(
                InstrumentClassification.instrument_id == instrument_id,
                InstrumentClassification.is_retracted.is_(False),
            )
        ).all()
    )


def _pick_winners(rows: list[InstrumentClassification]) -> dict[str, ResolvedField]:
    """Reduce one instrument's rows to the highest-ranked provenance per field.

    Ties cannot occur: the unique constraint allows one row per (instrument, field, provenance).
    """
    winners: dict[str, ResolvedField] = {}
    for row in rows:
        provenance = Provenance(row.provenance)
        current = winners.get(row.field)
        if current is not None and (
            PROVENANCE_RANK[current.provenance] >= PROVENANCE_RANK[provenance]
        ):
            continue
        winners[row.field] = ResolvedField(
            field=row.field,
            value=row.value,
            provenance=provenance,
            source=row.source,
            effective_at=row.effective_at,
            confidence=row.confidence,
            note=row.note,
        )
    return winners


def resolve_classification(
    session: Session, instrument_id: str
) -> tuple[dict[str, ResolvedField], list[str]]:
    """Pick the highest-ranked provenance per field, with the unresolved ones named."""
    winners = _pick_winners(_classification_rows(session, instrument_id))

    warnings: list[str] = []
    for field in sorted(CLASSIFICATION_FIELDS):
        resolved = winners.get(field)
        if resolved is None or resolved.value in (None, "", AssetClass.UNCLASSIFIED.value):
            warnings.append(f"{field} is unclassified")
    return winners, warnings


def resolve_field_for_many(
    session: Session, instrument_ids: list[str], *, field: str
) -> dict[str, ResolvedField]:
    """Resolve one classification field for many instruments in a single query.

    A consolidated summary reports every holding at once, so calling `resolve_classification` per
    row would cost a query per position. The winner rule is shared with the single-instrument
    path rather than restated, so the two cannot disagree about which provenance wins.

    Instruments with no row for the field are absent from the result: an unresolved field is
    reported as unclassified by the caller, never defaulted to a plausible value.
    """
    unique = {instrument_id for instrument_id in instrument_ids if instrument_id}
    if not unique:
        return {}

    rows = session.scalars(
        select(InstrumentClassification).where(
            InstrumentClassification.instrument_id.in_(unique),
            InstrumentClassification.field == field,
            InstrumentClassification.is_retracted.is_(False),
        )
    ).all()

    by_instrument: dict[str, list[InstrumentClassification]] = {}
    for row in rows:
        by_instrument.setdefault(row.instrument_id, []).append(row)

    resolved: dict[str, ResolvedField] = {}
    for instrument_id, instrument_rows in by_instrument.items():
        winner = _pick_winners(instrument_rows).get(field)
        if winner is not None:
            resolved[instrument_id] = winner
    return resolved


def record_classification(
    session: Session,
    instrument_id: str,
    *,
    field: str,
    value: str | None,
    provenance: Provenance,
    source: str,
    effective_at: datetime | None = None,
    fetched_at: datetime | None = None,
    confidence: Decimal | None = None,
    note: str | None = None,
) -> InstrumentClassification:
    """Upsert one (instrument, field, provenance) row, leaving other provenances untouched."""
    if field not in CLASSIFICATION_FIELDS:
        raise DomainError(
            422,
            "unknown_classification_field",
            "Classification field is not part of the taxonomy",
            {"field": field, "supported": sorted(CLASSIFICATION_FIELDS)},
        )
    now = utc_now()
    existing = session.scalar(
        select(InstrumentClassification).where(
            InstrumentClassification.instrument_id == instrument_id,
            InstrumentClassification.field == field,
            InstrumentClassification.provenance == provenance.value,
        )
    )
    if existing is not None:
        existing.value = value
        existing.source = source
        existing.effective_at = effective_at or now
        existing.fetched_at = fetched_at
        existing.confidence = confidence
        existing.note = note
        existing.is_retracted = False
        existing.updated_at = now
        return existing

    row = InstrumentClassification(
        id=str(uuid4()),
        instrument_id=instrument_id,
        field=field,
        value=value,
        provenance=provenance.value,
        source=source,
        effective_at=effective_at or now,
        fetched_at=fetched_at,
        confidence=confidence,
        note=note,
        is_retracted=False,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    return row


def apply_provider_classification(
    session: Session, instrument: Instrument, quote_type: str | None
) -> list[str]:
    """Translate provider metadata into DERIVED classification rows.

    Called on every market refresh. It only ever writes at DERIVED rank, so a manual override or
    verified mapping recorded earlier keeps winning.
    """
    derived = derive_from_provider(quote_type, ticker=instrument.ticker)
    source = f"{YAHOO_PROVIDER}:quoteType={quote_type or 'missing'}"
    now = utc_now()

    record_classification(
        session,
        instrument.instrument_id,
        field="asset_class",
        value=derived.asset_class.value,
        provenance=Provenance.DERIVED,
        source=source,
        fetched_at=now,
    )
    record_classification(
        session,
        instrument.instrument_id,
        field="security_type",
        value=derived.security_type.value,
        provenance=Provenance.DERIVED,
        source=source,
        fetched_at=now,
    )
    instrument.is_fund = derived.is_fund
    return list(derived.warnings)


def get_instrument_by_id(session: Session, instrument_id: str) -> Instrument:
    instrument = session.scalar(
        select(Instrument).where(Instrument.instrument_id == instrument_id)
    )
    if instrument is None:
        raise not_found("instrument", instrument_id)
    return instrument


def resolve_instrument(session: Session, reference: str) -> Instrument:
    """Resolve a ticker, stable instrument ID, or provider alias to one instrument."""
    candidate = reference.strip()
    if not candidate:
        raise not_found("instrument", reference)

    instrument = session.get(Instrument, candidate.upper())
    if instrument is not None:
        return instrument

    instrument = session.scalar(
        select(Instrument).where(Instrument.instrument_id == candidate)
    )
    if instrument is not None:
        return instrument

    alias = session.scalar(
        select(InstrumentAlias).where(
            InstrumentAlias.provider_symbol == candidate.upper(),
            InstrumentAlias.effective_to.is_(None),
        )
    )
    if alias is not None:
        return get_instrument_by_id(session, alias.instrument_id)
    raise not_found("instrument", reference)


def ensure_alias(
    session: Session, instrument: Instrument, provider: str, provider_symbol: str
) -> None:
    """Record a provider symbol for an instrument if it is not already tracked."""
    symbol = provider_symbol.strip().upper()
    existing = session.scalar(
        select(InstrumentAlias).where(
            InstrumentAlias.instrument_id == instrument.instrument_id,
            InstrumentAlias.provider == provider,
            InstrumentAlias.provider_symbol == symbol,
        )
    )
    if existing is not None:
        return
    session.add(
        InstrumentAlias(
            id=str(uuid4()),
            instrument_id=instrument.instrument_id,
            provider=provider,
            provider_symbol=symbol,
            exchange=instrument.exchange,
            effective_from=utc_now(),
            created_at=utc_now(),
        )
    )


def build_instrument_profile(session: Session, reference: str) -> InstrumentProfileRead:
    """Assemble stable identity, issuer, classification provenance, and coverage warnings."""
    instrument = resolve_instrument(session, reference)
    resolved, warnings = resolve_classification(session, instrument.instrument_id)
    aliases = session.scalars(
        select(InstrumentAlias)
        .where(InstrumentAlias.instrument_id == instrument.instrument_id)
        .order_by(InstrumentAlias.provider_symbol)
    ).all()
    issuer = session.get(Issuer, instrument.issuer_id) if instrument.issuer_id else None
    if issuer is None:
        warnings.append("issuer is unmapped; cross-listing exposure cannot aggregate")

    return InstrumentProfileRead(
        instrument_id=instrument.instrument_id,
        ticker=instrument.ticker,
        name=instrument.name,
        currency=instrument.currency,
        market=instrument.market,
        exchange=instrument.exchange,
        is_fund=instrument.is_fund,
        asset_type=instrument.asset_type,
        issuer=IssuerRead.model_validate(issuer) if issuer else None,
        classification={
            field: ClassificationFieldRead(
                value=item.value,
                provenance=item.provenance,
                source=item.source,
                effective_at=item.effective_at,
                confidence=item.confidence,
                note=item.note,
            )
            for field, item in sorted(resolved.items())
        },
        aliases=[InstrumentAliasRead.model_validate(alias) for alias in aliases],
        warnings=warnings,
    )


def set_classification_override(
    session: Session,
    reference: str,
    *,
    field: str,
    value: str | None,
    reason: str,
    effective_at: datetime | None = None,
    retract: bool = False,
) -> Instrument:
    """Record or retract a manual override. Provider rows are never modified."""
    instrument = resolve_instrument(session, reference)
    _validate_classification_value(field, value, retract=retract)

    if retract:
        existing = session.scalar(
            select(InstrumentClassification).where(
                InstrumentClassification.instrument_id == instrument.instrument_id,
                InstrumentClassification.field == field,
                InstrumentClassification.provenance == Provenance.MANUAL_OVERRIDE.value,
            )
        )
        if existing is None:
            raise not_found("classification_override", f"{instrument.ticker}:{field}")
        existing.is_retracted = True
        existing.note = reason
        existing.updated_at = utc_now()
        return instrument

    record_classification(
        session,
        instrument.instrument_id,
        field=field,
        value=value,
        provenance=Provenance.MANUAL_OVERRIDE,
        source="manual",
        effective_at=effective_at,
        note=reason,
    )
    return instrument


def _validate_classification_value(field: str, value: str | None, *, retract: bool) -> None:
    """Reject values outside the taxonomy so a typo cannot become a silent category."""
    if retract:
        return
    if field == "asset_class" and value not in {member.value for member in AssetClass}:
        raise DomainError(
            422,
            "invalid_classification_value",
            "asset_class must be a taxonomy member",
            {"value": value, "supported": [member.value for member in AssetClass]},
        )
    if field == "security_type" and value not in {member.value for member in SecurityType}:
        raise DomainError(
            422,
            "invalid_classification_value",
            "security_type must be a taxonomy member",
            {"value": value, "supported": [member.value for member in SecurityType]},
        )
    if field == "is_cash_equivalent" and value not in {"true", "false"}:
        raise DomainError(
            422,
            "invalid_classification_value",
            "is_cash_equivalent must be 'true' or 'false'",
            {"value": value},
        )


def map_issuer(
    session: Session,
    reference: str,
    *,
    legal_name: str,
    display_name: str | None = None,
    country_of_domicile: str | None = None,
    lei: str | None = None,
    issuer_id: str | None = None,
) -> tuple[Instrument, Issuer]:
    """Attach an instrument to an issuer, creating or reusing the issuer entity.

    Distinct listings of one company (TSM, 2330.TW) stay distinct instruments; only the issuer
    link is shared, which is what lets issuer-level exposure aggregate without losing the listings.
    """
    instrument = resolve_instrument(session, reference)
    now = utc_now()

    if issuer_id is not None:
        issuer = session.get(Issuer, issuer_id)
        if issuer is None:
            raise not_found("issuer", issuer_id)
    else:
        issuer = session.scalar(select(Issuer).where(Issuer.legal_name == legal_name))
        if issuer is None:
            issuer = Issuer(
                id=str(uuid4()),
                legal_name=legal_name,
                display_name=display_name or legal_name,
                country_of_domicile=country_of_domicile,
                lei=lei,
                created_at=now,
                updated_at=now,
            )
            session.add(issuer)
            session.flush()
        else:
            if display_name is not None:
                issuer.display_name = display_name
            if country_of_domicile is not None:
                issuer.country_of_domicile = country_of_domicile
            if lei is not None:
                issuer.lei = lei
            issuer.updated_at = now

    instrument.issuer_id = issuer.id
    instrument.updated_at = now
    return instrument, issuer
