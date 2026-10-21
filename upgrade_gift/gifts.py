"""Разбор ответов MTProto об уникальных (улучшенных) подарках."""
import re
from dataclasses import dataclass, field

from telethon.tl import types


@dataclass
class Attribute:
    name: str
    rarity: float | None  # в процентах
    rarity_label: str


@dataclass
class Owner:
    id: int | None = None
    username: str | None = None
    name: str | None = None
    ton_address: str | None = None

    @property
    def hidden(self) -> bool:
        return not (self.id or self.username or self.ton_address)


@dataclass
class GiftEvent:
    gift_id: int
    title: str
    slug: str
    num: int
    issued: int
    total: int
    owner: Owner
    transferable: bool
    transfer_status: str
    model: Attribute | None = None
    backdrop: Attribute | None = None
    pattern: Attribute | None = None
    floor_usd: float | None = None
    floor_stars: int | None = None
    extra: dict = field(default_factory=dict)

    @property
    def link(self) -> str:
        return f"https://t.me/nft/{self.slug}"


def slug_base(title: str) -> str:
    """'Plush Pepe' -> 'PlushPepe', "Durov's Cap" -> 'DurovsCap'."""
    return re.sub(r"[^0-9A-Za-z]", "", title)


_RARITY_LABELS = {
    "StarGiftAttributeRarityUncommon": "необычная",
    "StarGiftAttributeRarityRare": "редкая",
    "StarGiftAttributeRarityEpic": "эпическая",
    "StarGiftAttributeRarityLegendary": "легендарная",
}


def _attribute(attr) -> Attribute:
    rarity = getattr(attr, "rarity", None)
    permille = getattr(rarity, "permille", None)
    if permille is not None:
        pct = permille / 10
        return Attribute(attr.name, pct, f"{pct:g}%")
    label = _RARITY_LABELS.get(type(rarity).__name__, "")
    return Attribute(attr.name, None, label)


def attribute_info(attr) -> dict:
    a = _attribute(attr)
    return {"name": a.name, "rarity": a.rarity, "label": a.rarity_label}


def format_amount(amount) -> str:
    """StarsAmount / StarsTonAmount -> текст."""
    if isinstance(amount, types.StarsTonAmount):
        return f"{amount.amount / 1e9:g} TON"
    return f"{getattr(amount, 'amount', 0):,} ⭐".replace(",", " ")


def _peer_id(peer) -> int | None:
    for attr in ("user_id", "channel_id", "chat_id"):
        if hasattr(peer, attr):
            return getattr(peer, attr)
    return None


def _resolve_owner(gift, users, chats) -> Owner:
    owner = Owner(name=gift.owner_name, ton_address=gift.owner_address)
    if gift.owner_id is None:
        return owner
    pid = _peer_id(gift.owner_id)
    owner.id = pid
    if isinstance(gift.owner_id, types.PeerUser):
        for user in users:
            if user.id == pid:
                owner.username = user.username or next(
                    (u.username for u in (user.usernames or []) if u.active), None
                )
                full = " ".join(p for p in (user.first_name, user.last_name) if p)
                owner.name = full or owner.name
                break
    else:
        for chat in chats:
            if chat.id == pid:
                owner.username = getattr(chat, "username", None)
                owner.name = getattr(chat, "title", None) or owner.name
                break
    return owner


def parse_unique_gift(result) -> GiftEvent | None:
    """result — payments.UniqueStarGift."""
    gift = result.gift
    if not isinstance(gift, types.StarGiftUnique):
        return None

    owner = _resolve_owner(gift, result.users, result.chats)
    attrs = {}
    for attr in gift.attributes:
        if isinstance(attr, types.StarGiftAttributeModel):
            attrs["model"] = _attribute(attr)
        elif isinstance(attr, types.StarGiftAttributeBackdrop):
            attrs["backdrop"] = _attribute(attr)
        elif isinstance(attr, types.StarGiftAttributePattern):
            attrs["pattern"] = _attribute(attr)

    # Статус передачи по публичным данным подарка
    if gift.burned:
        transferable, status = False, "🔥 Сожжён"
    elif gift.owner_address or gift.gift_address:
        transferable, status = False, "⛓ Выведен в TON — передача только через блокчейн"
    elif gift.resell_amount:
        price = " / ".join(format_amount(a) for a in gift.resell_amount)
        transferable, status = True, f"🏷 Выставлен на продажу за {price}"
    else:
        transferable, status = True, "✅ Доступен к передаче"

    return GiftEvent(
        gift_id=gift.gift_id,
        title=gift.title,
        slug=gift.slug,
        num=gift.num,
        issued=gift.availability_issued,
        total=gift.availability_total,
        owner=owner,
        transferable=transferable,
        transfer_status=status,
        **attrs,
    )
