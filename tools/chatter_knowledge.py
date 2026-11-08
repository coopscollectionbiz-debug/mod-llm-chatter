"""Chatter 2.0 gameplay knowledge engine.

Answers factual gameplay questions during ordinary normal-mode chat from the
realm's own world database, including custom content. There is no command,
no tool loop and no model planning call: intents are recognised
deterministically from native WoW links and question shapes, then resolved
with a few bounded, parameterised, read-only queries inside the reply
deadline.

The exact-ID predicates, the creature/gameobject starter union and the
evidence/uncertainty pattern are adapted from Hokken's mod-llm-guide
(commit 5fe0819eec25afd58a3c601e871f1de12f2f5ccd, ``tools/guide_tool_quests.py``
``_get_quest_info``/``_get_available_quests`` and ``tools/guide_reliability.py``),
GNU AGPL v3, rewritten for this module on 2026-11-08. Guide's command
interface, executor, fuzzy boss-loot lookup and configured rare-spawn
counting are intentionally NOT used: configured data can never establish
that a rare is alive, that a quest is available to a character, or a drop
chance through references and conditions.

Every result is an ``Evidence`` record with ``source='server_data'`` and an
explicit status. Configured facts describe the realm's data, not live
availability. Individual Progression and ``conditions`` rows can gate
quests and loot per character, so eligibility is always reported as
unchecked.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import logging
import re
import threading
import time

from chatter_evidence import Evidence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent recognition
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(
    r'\|c[0-9a-fA-F]{8}\|H(quest|item|spell):(\d+)((?::-?\d+)*)\|h\[([^\]|]{1,100})\]\|h\|r'
)
# Plain-text quest/item mentions are never resolved by title: same-name quests
# with different IDs exist, so only native links (exact IDs) are trusted.

_START = re.compile(r"\b(who|where|how)\b.*\b(start|starts|get|gets|pick(?:ed)? up|give|gives|got)\b|\bquest ?giver\b", re.I)
_TURN_IN = re.compile(r"\b(turn(?:s|ed)? (?:it )?in|hand(?:s|ed)? (?:it )?in|ender|where do i (?:take|bring))\b", re.I)
_QUEST_DETAIL = re.compile(r"\b(level|lvl|req|requirement|prereq|before|after|chain|next|objective|need|what do|how many|kill|collect)\b", re.I)
_DROP = re.compile(r"\b(drop|drops|dropped|droppin[g']?|loot|from|farm|source|where (?:do|can) (?:i|you) get|where.*get)\b", re.I)
_ITEM_DETAIL = re.compile(r"\b(stats|good|upgrade|use|usable|equip|wear|can i|level|req|bis|worth)\b", re.I)
_THIS_BOSS = re.compile(r"\b(this|that|the|next|last) (boss|guy|one)\b|\b(he|she|it) drop", re.I)
_BOSS_LIST = re.compile(r"\b(which|what|who|how many)\b.*\bboss(es)?\b|\bbosses (here|in here|left)\b", re.I)
_SERVICE = re.compile(
    r"\b(where|any|nearest|closest|is there|anyone know)\b.*\b(trainer|vendor|repair|flight ?master|fp|innkeeper|inn|"
    r"auctioneer|ah|banker|bank|stable ?master)\b", re.I)
_CLASSES = ('warrior', 'paladin', 'hunter', 'rogue', 'priest', 'death knight', 'shaman', 'mage', 'warlock', 'druid')
_PROFESSIONS = ('alchemy', 'blacksmithing', 'enchanting', 'engineering', 'herbalism', 'inscription', 'jewelcrafting',
                'leatherworking', 'mining', 'skinning', 'tailoring', 'cooking', 'first aid', 'fishing', 'riding')
_ELIGIBLE = re.compile(r"\b(can i|could i|am i able|eligible|available to me|can we)\b.*\b(take|accept|get|do|start)\b", re.I)

# creature_template.npcflag bits (AzerothCore UnitNPCFlags).
NPCFLAG = {
    'gossip': 0x1, 'questgiver': 0x2, 'trainer': 0x10, 'vendor': 0x80, 'repair': 0x1000,
    'flightmaster': 0x2000, 'innkeeper': 0x10000, 'banker': 0x20000, 'auctioneer': 0x200000,
    'stablemaster': 0x400000,
}
_SERVICE_FLAG = {
    'trainer': 'trainer', 'vendor': 'vendor', 'repair': 'repair', 'flight master': 'flightmaster',
    'flightmaster': 'flightmaster', 'fp': 'flightmaster', 'innkeeper': 'innkeeper', 'inn': 'innkeeper',
    'auctioneer': 'auctioneer', 'ah': 'auctioneer', 'banker': 'banker', 'bank': 'banker',
    'stable master': 'stablemaster', 'stablemaster': 'stablemaster',
}
CREATURE_RANK = {0: 'normal', 1: 'elite', 2: 'rare elite', 3: 'boss', 4: 'rare'}
ITEM_QUALITY = {0: 'poor', 1: 'common', 2: 'uncommon', 3: 'rare', 4: 'epic', 5: 'legendary', 6: 'artifact',
                7: 'heirloom'}


@dataclass
class Intent:
    kind: str
    entity_id: int = 0
    label: str = ''
    extra: dict = field(default_factory=dict)


def parse_links(message):
    """Native quest/item/spell links only; anything malformed is ignored."""
    links = []
    for match in _LINK_RE.finditer(message or ''):
        links.append({'type': match.group(1), 'id': int(match.group(2)), 'label': match.group(4)})
    return links[:3]


def strip_links(message):
    return _LINK_RE.sub(lambda m: f'[{m.group(4)}]', message or '')


def detect_intents(message, links=None):
    """Map one chat turn to lookups. Deterministic; empty means no lookup."""
    links = parse_links(message) if links is None else links
    text = strip_links(message).casefold()
    intents = []
    quests = [l for l in links if l['type'] == 'quest']
    items = [l for l in links if l['type'] == 'item']
    for quest in quests[:2]:
        if _ELIGIBLE.search(text):
            intents.append(Intent('quest_eligibility', quest['id'], quest['label']))
        if _TURN_IN.search(text):
            intents.append(Intent('quest_ender', quest['id'], quest['label']))
        elif _START.search(text):
            intents.append(Intent('quest_starter', quest['id'], quest['label']))
        elif _QUEST_DETAIL.search(text) or '?' in text:
            intents.append(Intent('quest_info', quest['id'], quest['label']))
    for item in items[:2]:
        if _DROP.search(text):
            boss = bool(_THIS_BOSS.search(text))
            intents.append(Intent('boss_drop' if boss else 'item_source', item['id'], item['label'],
                                  {'boss_text': text} if boss else {}))
        elif _ITEM_DETAIL.search(text) or '?' in text:
            intents.append(Intent('item_info', item['id'], item['label']))
    if not links:
        service = _SERVICE.search(text)
        if service:
            word = service.group(2).casefold()
            extra = {'flag': _SERVICE_FLAG.get(re.sub(r'\s+', ' ', word), 'trainer')}
            for name in _CLASSES + _PROFESSIONS:
                if re.search(rf'\b{name}\b', text):
                    extra['specialty'] = name
                    break
            intents.append(Intent('npc_service', extra=extra))
        elif _BOSS_LIST.search(text):
            intents.append(Intent('boss_list'))
    return intents[:3]


# ---------------------------------------------------------------------------
# Read-only world access
# ---------------------------------------------------------------------------

class KnowledgeTimeout(Exception):
    """Raised when the reply deadline leaves no time for a lookup."""


class WorldReader:
    """Pooled, read-only, time-capped access to the world database.

    ``connect`` returns a DB-API connection; it is injectable for tests.
    Each pooled connection is set READ ONLY with a server-side statement
    timeout (MySQL ``MAX_EXECUTION_TIME``).
    """

    def __init__(self, config, connect=None, pool_size=2):
        self._config = config
        self._connect = connect or self._default_connect
        self._pool = []
        self._lock = threading.Lock()
        self._size = max(1, pool_size)
        self.statement_ms = max(50, int(config.get('LLMChatter.Knowledge.QueryTimeoutMs', 300)))
        self._revision = (0.0, '')
        self.queries = 0

    def _default_connect(self):
        import mysql.connector
        cfg = self._config
        return mysql.connector.connect(
            host=cfg.get('LLMChatter.Database.Host', 'localhost'),
            port=int(cfg.get('LLMChatter.Database.Port', 3306)),
            user=cfg.get('LLMChatter.Knowledge.User') or cfg.get('LLMChatter.Database.User', 'acore'),
            password=(cfg.get('LLMChatter.Knowledge.Password')
                      if cfg.get('LLMChatter.Knowledge.User') else cfg.get('LLMChatter.Database.Password', 'acore')),
            database=cfg.get('LLMChatter.Knowledge.WorldDatabase', 'acore_world'),
            connection_timeout=max(1, int(cfg.get('LLMChatter.Knowledge.ConnectTimeoutSeconds', 2))),
            autocommit=True,
        )

    def _acquire(self):
        with self._lock:
            if self._pool:
                return self._pool.pop()
        conn = self._connect()
        cursor = conn.cursor()
        try:
            cursor.execute('SET SESSION TRANSACTION READ ONLY')
            cursor.execute(f'SET SESSION MAX_EXECUTION_TIME = {int(self.statement_ms)}')
        finally:
            cursor.close()
        return conn

    def _release(self, conn, broken=False):
        if broken:
            try:
                conn.close()
            except Exception:
                pass
            return
        with self._lock:
            if len(self._pool) < self._size:
                self._pool.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def query(self, sql, params=(), deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            raise KnowledgeTimeout('reply deadline reached')
        conn = self._acquire()
        broken = False
        try:
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            finally:
                cursor.close()
            self.queries += 1
            return rows
        except Exception:
            broken = True
            raise
        finally:
            self._release(conn, broken)

    def revision(self):
        """World-data revision key: applied update count + latest timestamp."""
        now = time.monotonic()
        cached_at, value = self._revision
        if value and now - cached_at < 60:
            return value
        try:
            row = self.query('SELECT COUNT(*) AS n, MAX(timestamp) AS t FROM updates')[0]
            value = f"{row['n']}:{row['t']}"
        except Exception:
            value = value or 'unknown'
        self._revision = (now, value)
        return value

    def close(self):
        with self._lock:
            pool, self._pool = self._pool, []
        for conn in pool:
            try:
                conn.close()
            except Exception:
                pass


class TTLCache:
    """Bounded LRU keyed by world revision; entries also expire by age."""

    def __init__(self, size=2048, ttl=600):
        self._data = OrderedDict()
        self._size = size
        self._ttl = ttl
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self._lock:
            item = self._data.get(key)
            if item and time.monotonic() - item[0] <= self._ttl:
                self._data.move_to_end(key)
                self.hits += 1
                return item[1]
            if item:
                del self._data[key]
            self.misses += 1
            return None

    def put(self, key, value):
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._size:
                self._data.popitem(last=False)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def _server(kind, status, facts, note=''):
    return Evidence(kind=kind, source='server_data', status=status, facts=facts, audience='public', note=note)


def _approx_zone(map_id, x, y):
    from chatter_constants import ZONE_COORDINATES
    from chatter_shared import get_zone_name
    for zone_id, (zmap, min_x, max_x, min_y, max_y) in ZONE_COORDINATES.items():
        if zmap == map_id and min_x <= x <= max_x and min_y <= y <= max_y:
            return get_zone_name(zone_id) or ''
    return ''


def _place(reader, table, entry, deadline):
    """Configured spawn location label for a creature or gameobject entry."""
    rows = reader.query(
        f'SELECT map, zoneId, position_x AS x, position_y AS y FROM {table} WHERE id = %s LIMIT 3',
        (entry,), deadline)
    if not rows:
        return {'spawn': 'no configured spawn'}
    from chatter_shared import get_zone_name
    row = rows[0]
    zone = get_zone_name(int(row['zoneId'])) if int(row['zoneId'] or 0) else ''
    zone = zone or _approx_zone(int(row['map']), float(row['x']), float(row['y']))
    place = {'spawn_map': int(row['map'])}
    if zone:
        place['spawn_zone'] = zone
    if len(rows) > 1:
        place['spawn_count'] = f'{len(rows)}+' if len(rows) == 3 else len(rows)
    return place


def _conditions(reader, source_types, entry, deadline):
    rows = reader.query(
        'SELECT COUNT(*) AS n FROM conditions WHERE SourceTypeOrReferenceId IN (%s) AND SourceEntry = %%s'
        % ','.join(str(int(t)) for t in source_types), (entry,), deadline)
    return int(rows[0]['n']) if rows else 0


def _quest_row(reader, quest_id, deadline):
    rows = reader.query(
        'SELECT q.ID, q.LogTitle, q.QuestLevel, q.MinLevel, q.AllowableRaces, q.Flags, '
        'q.ObjectiveText1, q.ObjectiveText2, q.ObjectiveText3, q.ObjectiveText4, '
        'q.RequiredNpcOrGo1, q.RequiredNpcOrGo2, q.RequiredNpcOrGo3, q.RequiredNpcOrGo4, '
        'q.RequiredNpcOrGoCount1, q.RequiredNpcOrGoCount2, q.RequiredNpcOrGoCount3, q.RequiredNpcOrGoCount4, '
        'q.RequiredItemId1, q.RequiredItemId2, q.RequiredItemId3, q.RequiredItemId4, '
        'q.RequiredItemCount1, q.RequiredItemCount2, q.RequiredItemCount3, q.RequiredItemCount4, '
        'a.PrevQuestID, a.NextQuestID, a.ExclusiveGroup, a.AllowableClasses, a.MaxLevel, '
        'a.BreadcrumbForQuestId '
        'FROM quest_template q LEFT JOIN quest_template_addon a ON a.ID = q.ID WHERE q.ID = %s',
        (quest_id,), deadline)
    return rows[0] if rows else None


def _label_check(facts, label, title):
    """A link's display label is untrusted; the ID decides what it is."""
    if label and title and label.strip().casefold() != title.strip().casefold():
        facts['link_label_mismatch'] = True
        return 'link label differs from the real title; use the real title'
    return ''


def _same_title(reader, quest_id, title, deadline):
    rows = reader.query('SELECT COUNT(*) AS n FROM quest_template WHERE LogTitle = %s AND ID <> %s',
                        (title, quest_id), deadline)
    return int(rows[0]['n']) if rows else 0


def lookup_quest_starters(reader, intent, deadline, ender=False):
    quest = _quest_row(reader, intent.entity_id, deadline)
    kind = 'quest'
    if not quest:
        return [_server(kind, 'not_found', {'quest_id': intent.entity_id, 'ask': 'enders' if ender else 'starters'})]
    title = quest['LogTitle']
    suffix = 'questender' if ender else 'queststarter'
    rows = reader.query(
        f'(SELECT \'npc\' AS type, ct.entry, ct.name FROM creature_{suffix} s '
        f'JOIN creature_template ct ON ct.entry = s.id WHERE s.quest = %s LIMIT 5) '
        f'UNION ALL (SELECT \'object\' AS type, gt.entry, gt.name FROM gameobject_{suffix} s '
        f'JOIN gameobject_template gt ON gt.entry = s.id WHERE s.quest = %s LIMIT 5)',
        (intent.entity_id, intent.entity_id), deadline)
    if not ender:
        rows += reader.query(
            "SELECT 'item' AS type, entry, name FROM item_template WHERE startquest = %s LIMIT 3",
            (intent.entity_id,), deadline)
    facts = {'quest_id': intent.entity_id, 'title': title}
    note = _label_check(facts, intent.label, title)
    others = _same_title(reader, intent.entity_id, title, deadline)
    if others:
        facts['same_title_other_ids'] = others
    role = 'turn_in_to' if ender else 'started_by'
    if not rows:
        facts[role] = []
        return [_server(kind, 'not_found', facts, note or 'no configured NPC/object/item')]
    entries = []
    for row in rows[:5]:
        entry = {'type': row['type'], 'name': row['name']}
        if row['type'] in ('npc', 'object'):
            entry.update(_place(reader, 'creature' if row['type'] == 'npc' else 'gameobject',
                                int(row['entry']), deadline))
        entries.append(entry)
    facts[role] = entries
    if not ender:
        facts['eligibility'] = 'not checked (progression, conditions and phase can gate it)'
    status = 'ambiguous' if len(rows) > 1 and not ender else 'found'
    if len(rows) > 1:
        note = (note + '; ' if note else '') + 'several configured starters' if not ender else note
    return [_server(kind, status if not ender else 'found', facts, note)]


def _names(reader, table, key, ids, deadline):
    ids = sorted({int(i) for i in ids if int(i or 0) > 0})
    if not ids:
        return {}
    rows = reader.query(
        f'SELECT {key} AS id, name FROM {table} WHERE {key} IN ({",".join(["%s"] * len(ids))})',
        tuple(ids), deadline)
    return {int(r['id']): r['name'] for r in rows}


def lookup_quest_info(reader, intent, deadline):
    quest = _quest_row(reader, intent.entity_id, deadline)
    if not quest:
        return [_server('quest', 'not_found', {'quest_id': intent.entity_id})]
    facts = {'quest_id': intent.entity_id, 'title': quest['LogTitle'],
             'quest_level': quest['QuestLevel'], 'min_level': quest['MinLevel']}
    note = _label_check(facts, intent.label, quest['LogTitle'])
    npcs = [quest[f'RequiredNpcOrGo{i}'] for i in range(1, 5)]
    creature_names = _names(reader, 'creature_template', 'entry', [n for n in npcs if n > 0], deadline)
    object_names = _names(reader, 'gameobject_template', 'entry', [-n for n in npcs if n < 0], deadline)
    item_ids = [quest[f'RequiredItemId{i}'] for i in range(1, 5)]
    item_names = _names(reader, 'item_template', 'entry', item_ids, deadline)
    objectives = []
    for i in range(1, 5):
        npc, count = quest[f'RequiredNpcOrGo{i}'], quest[f'RequiredNpcOrGoCount{i}']
        if npc and count:
            name = creature_names.get(npc) if npc > 0 else object_names.get(-npc)
            objectives.append(f'{"kill" if npc > 0 else "use"} {count} {name or "unknown"}')
        item, count = quest[f'RequiredItemId{i}'], quest[f'RequiredItemCount{i}']
        if item and count:
            objectives.append(f'collect {count} {item_names.get(item, "unknown item")}')
        text = (quest[f'ObjectiveText{i}'] or '').strip()
        if text and not objectives:
            objectives.append(text[:80])
    facts['objectives'] = objectives[:6]
    chain = {}
    titles = _names(reader, 'quest_template', 'ID', [abs(quest['PrevQuestID'] or 0), quest['NextQuestID'] or 0],
                    deadline) if quest.get('PrevQuestID') is not None else {}
    if quest.get('PrevQuestID'):
        chain['after'] = titles.get(abs(quest['PrevQuestID']), f'quest {abs(quest["PrevQuestID"])}')
    if quest.get('NextQuestID'):
        chain['leads_to'] = titles.get(quest['NextQuestID'], f'quest {quest["NextQuestID"]}')
    if quest.get('BreadcrumbForQuestId'):
        chain['breadcrumb'] = True
    if chain:
        facts['chain'] = chain
    if quest.get('MaxLevel'):
        facts['max_level'] = quest['MaxLevel']
    gated = _conditions(reader, (19, 20), intent.entity_id, deadline)
    facts['eligibility'] = 'not checked' + (f' ({gated} configured conditions)' if gated else '')
    return [_server('quest', 'found', facts, note)]


def lookup_quest_eligibility(reader, intent, deadline):
    """The world database cannot decide whether *this* character can take it."""
    quest = _quest_row(reader, intent.entity_id, deadline)
    facts = {'quest_id': intent.entity_id, 'title': quest['LogTitle'] if quest else None,
             'min_level': quest['MinLevel'] if quest else None}
    return [_server('quest', 'unsupported', facts,
                    'whether a character can accept it needs that character, progression and phase checks')]


def _item_row(reader, item_id, deadline):
    rows = reader.query(
        'SELECT entry, name, Quality, class, subclass, InventoryType, ItemLevel, RequiredLevel, '
        'AllowableClass, AllowableRace, bonding, RequiredSkill, RequiredSkillRank, startquest '
        'FROM item_template WHERE entry = %s', (item_id,), deadline)
    return rows[0] if rows else None


def lookup_item_info(reader, intent, deadline):
    item = _item_row(reader, intent.entity_id, deadline)
    if not item:
        return [_server('item', 'not_found', {'item_id': intent.entity_id})]
    from chatter_constants import ARMOR_SUBCLASS_NAMES, ITEM_CLASS_NAMES, WEAPON_SUBCLASS_NAMES
    item_class = int(item['class'])
    subtype = (WEAPON_SUBCLASS_NAMES.get(int(item['subclass'])) if item_class == 2 else
               ARMOR_SUBCLASS_NAMES.get(int(item['subclass'])) if item_class == 4 else None)
    facts = {'item_id': item['entry'], 'name': item['name'],
             'quality': ITEM_QUALITY.get(int(item['Quality']), 'unknown'),
             'type': subtype or ITEM_CLASS_NAMES.get(item_class, 'item'),
             'item_level': item['ItemLevel'], 'required_level': item['RequiredLevel']}
    mask = int(item['AllowableClass'] or -1)
    if mask not in (-1, 0, 0x7FFF, 0x5FF, 0x7FF):
        from chatter_constants import CLASS_BITMASK
        allowed = [name for name, bit in CLASS_BITMASK.items() if mask & bit]
        if allowed:
            facts['classes'] = allowed
    if int(item['RequiredSkill'] or 0):
        facts['requires_skill_rank'] = item['RequiredSkillRank']
    if int(item['startquest'] or 0):
        facts['starts_quest'] = int(item['startquest'])
    note = _label_check(facts, intent.label, item['name'])
    return [_server('item', 'found', facts, note)]


def _expand_references(reader, item_id, deadline, depth=2):
    """Reference entries that (directly or through one nested level) hold the item."""
    found, frontier, truncated = set(), {('item', item_id)}, False
    for level in range(depth):
        items = [v for kind, v in frontier if kind == 'item']
        refs = [v for kind, v in frontier if kind == 'ref']
        rows = []
        if items:
            rows += reader.query('SELECT Entry FROM reference_loot_template WHERE Item = %s AND Reference = 0 LIMIT 40',
                                 (items[0],), deadline)
        if refs:
            rows += reader.query(
                f'SELECT Entry FROM reference_loot_template WHERE Reference IN ({",".join(["%s"] * len(refs))}) LIMIT 40',
                tuple(refs), deadline)
        new = {int(r['Entry']) for r in rows} - found
        if len(rows) >= 40:
            truncated = True
        found |= new
        frontier = {('ref', ref) for ref in new}
        if not frontier:
            break
    else:
        truncated = truncated or bool(frontier)
    return sorted(found)[:60], truncated


def lookup_item_source(reader, intent, deadline, restrict_creatures=None):
    """Configured loot/vendor sources. Never a drop rate, never proof of absence."""
    item = _item_row(reader, intent.entity_id, deadline)
    if not item:
        return [_server('loot', 'not_found', {'item_id': intent.entity_id})]
    refs, truncated = _expand_references(reader, intent.entity_id, deadline)
    creature_where = 'l.Item = %s AND l.Reference = 0'
    params = [intent.entity_id]
    if refs:
        creature_where = f'({creature_where}) OR l.Reference IN ({",".join(["%s"] * len(refs))})'
        params += refs
    restrict = ''
    if restrict_creatures:
        restrict = f' AND ct.entry IN ({",".join(["%s"] * len(restrict_creatures))})'
        params += list(restrict_creatures)
    rows = reader.query(
        f'SELECT ct.entry, ct.name, ct.rank, l.Chance, l.Reference, l.GroupId, l.QuestRequired, l.LootMode '
        f'FROM creature_loot_template l JOIN creature_template ct ON ct.lootid = l.Entry '
        f'WHERE ({creature_where}){restrict} LIMIT 12', tuple(params), deadline)
    chests = reader.query(
        'SELECT gt.name FROM gameobject_loot_template l JOIN gameobject_template gt '
        'ON gt.type = 3 AND gt.Data1 = l.Entry WHERE l.Item = %s AND l.Reference = 0 LIMIT 4',
        (intent.entity_id,), deadline) if not restrict_creatures else []
    vendors = reader.query(
        'SELECT ct.name FROM npc_vendor v JOIN creature_template ct ON ct.entry = v.entry '
        'WHERE v.item = %s LIMIT 4', (intent.entity_id,), deadline) if not restrict_creatures else []
    conditional = _conditions(reader, (1, 4, 10), intent.entity_id, deadline)
    sources = []
    for row in rows[:8]:
        source = {'from': row['name'], 'rank': CREATURE_RANK.get(int(row['rank']), 'normal')}
        if int(row['Reference'] or 0) or int(row['GroupId'] or 0):
            source['chance'] = 'shared loot group; not computed'
        elif row['Chance'] is not None:
            source['configured_chance_pct'] = round(float(row['Chance']), 2)
        if int(row['QuestRequired'] or 0):
            source['quest_only'] = True
        if int(row['LootMode'] or 1) != 1:
            source['loot_mode'] = int(row['LootMode'])
        sources.append(source)
    facts = {'item_id': intent.entity_id, 'name': item['name']}
    if sources:
        facts['creature_sources'] = sources
    if chests:
        facts['containers'] = [r['name'] for r in chests]
    if vendors:
        facts['vendors'] = [r['name'] for r in vendors]
    notes = ['configured sources only; scripted or encounter-chest drops may exist']
    status = 'found' if (sources or chests or vendors) else 'not_found'
    if truncated or len(rows) >= 12:
        status = 'partial'
        notes.append('loot references truncated')
    if conditional:
        status = 'partial' if status == 'found' else status
        notes.append(f'{conditional} loot conditions apply')
    label = _label_check(facts, intent.label, item['name'])
    if label:
        notes.insert(0, label)
    return [_server('loot', status, facts, '; '.join(notes))]


def lookup_bosses(reader, world, deadline):
    map_id = world.get('map_id')
    if map_id is None:
        return [_server('boss', 'unsupported', {}, 'current instance unknown')]
    rows = reader.query(
        'SELECT DISTINCT ct.entry, ct.name FROM creature c JOIN creature_template ct ON ct.entry = c.id '
        'WHERE c.map = %s AND (ct.rank = 3 OR ct.entry IN '
        '(SELECT creditEntry FROM instance_encounters WHERE creditType = 0)) LIMIT 16',
        (int(map_id),), deadline)
    if not rows:
        return [_server('boss', 'not_found', {'map_id': int(map_id)}, 'no configured bosses on this map')]
    return [_server('boss', 'found', {'map_id': int(map_id), 'bosses': [r['name'] for r in rows]},
                    'configured encounters; scripted bosses may differ')]


def _resolve_boss(reader, world, text, deadline):
    """The boss being discussed, only if unambiguous on the current map."""
    map_id = world.get('map_id')
    if map_id is None:
        return None, 'unsupported'
    rows = reader.query(
        'SELECT DISTINCT ct.entry, ct.name, ct.difficulty_entry_1, ct.difficulty_entry_2, ct.difficulty_entry_3 '
        'FROM creature c JOIN creature_template ct ON ct.entry = c.id WHERE c.map = %s AND (ct.rank = 3 OR '
        'ct.entry IN (SELECT creditEntry FROM instance_encounters WHERE creditType = 0)) LIMIT 24',
        (int(map_id),), deadline)
    named = [r for r in rows if re.search(rf"\b{re.escape(r['name'].split(',')[0].casefold())}\b", text)]
    target = world.get('target_entry')
    if not named and target:
        named = [r for r in rows if int(r['entry']) == int(target)]
    if len(named) == 1:
        return named[0], 'found'
    return None, 'ambiguous' if named or len(rows) > 1 else ('not_found' if not rows else 'ambiguous')


def lookup_boss_drop(reader, intent, world, deadline):
    boss, status = _resolve_boss(reader, world, intent.extra.get('boss_text', ''), deadline)
    if not boss:
        evidence = lookup_item_source(reader, intent, deadline)
        evidence[0].note = (evidence[0].note + '; ' if evidence[0].note else '') + 'boss not identified unambiguously'
        return evidence
    difficulty = int(world.get('difficulty') or 0)
    entries = [int(boss['entry'])]
    variant = boss.get(f'difficulty_entry_{difficulty}') if difficulty else 0
    if variant:
        entries = [int(variant)]
    evidence = lookup_item_source(reader, intent, deadline, restrict_creatures=entries)[0]
    evidence.facts['boss'] = boss['name']
    evidence.facts['difficulty'] = difficulty
    if evidence.status == 'not_found':
        # Absence from one boss's table is not absence: scripted chests,
        # conditions and other modes can still award it.
        evidence.status = 'partial'
        evidence.facts['on_boss_table'] = False
    else:
        evidence.facts['on_boss_table'] = True
    return [evidence]


def lookup_npc_service(reader, intent, world, deadline):
    flag = NPCFLAG.get(intent.extra.get('flag', 'trainer'), NPCFLAG['trainer'])
    x, y, map_id = world.get('x'), world.get('y'), world.get('map_id')
    if x is None or y is None or map_id is None:
        return [_server('npc_service', 'unsupported', {}, 'speaker position unknown')]
    radius = float(world.get('service_radius', 400))
    params = [int(map_id), float(x) - radius, float(x) + radius, float(y) - radius, float(y) + radius, flag]
    specialty = ''
    if intent.extra.get('specialty'):
        specialty = ' AND ct.subname LIKE %s'
        params.append(f'%{intent.extra["specialty"]}%')
    rows = reader.query(
        'SELECT ct.name, ct.subname, c.position_x AS x, c.position_y AS y FROM creature c '
        'JOIN creature_template ct ON ct.entry = c.id WHERE c.map = %s AND c.position_x BETWEEN %s AND %s '
        f'AND c.position_y BETWEEN %s AND %s AND (ct.npcflag & %s) <> 0{specialty} LIMIT 30',
        tuple(params), deadline)
    if not rows:
        return [_server('npc_service', 'not_found', {'service': intent.extra.get('flag')},
                        'nothing configured nearby; may exist further away')]
    for row in rows:
        row['d'] = ((float(row['x']) - float(x)) ** 2 + (float(row['y']) - float(y)) ** 2) ** 0.5
    rows.sort(key=lambda r: r['d'])
    found = [{'name': r['name'], 'title': r['subname'] or '', 'yards_straight_line': int(r['d'])}
             for r in rows[:3]]
    return [_server('npc_service', 'found', {'service': intent.extra.get('flag'), 'nearest': found},
                    'configured spawns; faction, phase and path not verified')]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class KnowledgeEngine:
    def __init__(self, config, reader=None):
        self.config = config
        self.enabled = str(config.get('LLMChatter.Knowledge.Enable', '1')) == '1'
        self.reader = reader or WorldReader(config, pool_size=int(config.get('LLMChatter.Knowledge.PoolSize', 2)))
        self.cache = TTLCache(int(config.get('LLMChatter.Knowledge.CacheSize', 2048)),
                              int(config.get('LLMChatter.Knowledge.CacheSeconds', 900)))
        self.max_lookups = max(1, int(config.get('LLMChatter.Knowledge.MaxLookupsPerTurn', 2)))

    def lookup(self, intents, world=None, deadline=None):
        """Resolve intents to evidence. Never raises; failures become 'unavailable'."""
        world = world or {}
        results, stats = [], {'lookups': 0, 'cache_hits': 0, 'failed': 0, 'ms': 0.0}
        if not self.enabled:
            return results, stats
        started = time.monotonic()
        for intent in intents[:self.max_lookups]:
            key = None
            if intent.kind not in ('npc_service', 'boss_list', 'boss_drop'):
                key = (self.reader.revision(), intent.kind, intent.entity_id, intent.label)
                cached = self.cache.get(key)
                if cached is not None:
                    stats['cache_hits'] += 1
                    results.extend(cached)
                    continue
            try:
                found = self._dispatch(intent, world, deadline)
                stats['lookups'] += 1
                if key is not None:
                    self.cache.put(key, found)
                results.extend(found)
            except Exception as exc:  # timeouts, DB errors, schema drift
                stats['failed'] += 1
                logger.info('knowledge lookup %s failed: %s', intent.kind, type(exc).__name__)
                results.append(_server(intent.kind.split('_')[0] if intent.kind.startswith(('quest', 'item'))
                                       else 'loot' if intent.kind == 'boss_drop' else intent.kind,
                                       'unavailable', {'id': intent.entity_id} if intent.entity_id else {},
                                       'lookup failed'))
        stats['ms'] = round((time.monotonic() - started) * 1000, 1)
        return results, stats

    def _dispatch(self, intent, world, deadline):
        reader = self.reader
        if intent.kind == 'quest_starter':
            return lookup_quest_starters(reader, intent, deadline)
        if intent.kind == 'quest_ender':
            return lookup_quest_starters(reader, intent, deadline, ender=True)
        if intent.kind == 'quest_info':
            return lookup_quest_info(reader, intent, deadline)
        if intent.kind == 'quest_eligibility':
            return lookup_quest_eligibility(reader, intent, deadline)
        if intent.kind == 'item_info':
            return lookup_item_info(reader, intent, deadline)
        if intent.kind == 'item_source':
            return lookup_item_source(reader, intent, deadline)
        if intent.kind == 'boss_drop':
            return lookup_boss_drop(reader, intent, world, deadline)
        if intent.kind == 'boss_list':
            return lookup_bosses(reader, world, deadline)
        if intent.kind == 'npc_service':
            return lookup_npc_service(reader, intent, world, deadline)
        return [_server(intent.kind, 'unsupported', {})]


_engine = None
_engine_lock = threading.Lock()


def configure(config, reader=None):
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.reader.close()
        _engine = KnowledgeEngine(config, reader)
    return _engine


def engine():
    return _engine
