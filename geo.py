"""Where a job is (Nov 1 2026). Replaces the seven-name LOCAL list with commute regions.

resolve(location, remote=False, text="") -> Place(status, region, tier, city)
  status: "local"     in an enabled commute region (NYC, Westchester, Fairfield County, North Jersey, Long Island ...)
          "remote_us" remote and open to someone in New York (US-wide, or limited to NY/NJ/CT)
          "maybe"     can't tell yet: "2 Locations" (Workday), "Multiple locations", a state-wide NJ/CT posting, blank
          "out"       somewhere else, abroad, remote limited to another state, or a region whose tier is switched off
Remote eligibility is decided separately from commute regions (ALLOW_REMOTE_US).

Formats handled: "New York, NY, USA", "US-NY-New York", "NY - New York, 1407 Broadway", "United States-New York-New York",
"New York City Metropolitan Area", "Yonkers, NY 10701", "Bergen County, NJ", "Stamford, CT, us" (lower-case = ISO country,
as SmartRecruiters and JobSpy write it), "Lyon, Auvergne-Rhone-Alpes, fr", multi-location strings split on ; | / • or.
"Albany, New York" is not New York City: "New York" after a city is the state.
"""
import functools
import re
from collections import namedtuple

Place = namedtuple("Place", "status region tier city")

# Tiers switched on. Add "stretch" to take in Danbury/Bridgeport, Princeton, outer Long Island and similar 45-75 minute commutes.
ENABLED_TIERS = {"core", "commute"}
ALLOW_REMOTE_US = True
HOME_STATES = {"ny", "nj", "ct"}          # remote jobs limited to these states are still open to him

# region: (label, tier, state, places). A trailing * marks a name common elsewhere ("Greenwich", "Norwalk", "Madison"): it
# counts only when the location also names the state.
REGIONS = {
    "nyc": ("New York City", "core", "ny", """
        new york city, nyc, manhattan, brooklyn, queens, bronx, the bronx, staten island, long island city, lic, astoria*, flushing*,
        jamaica*, harlem*, east harlem, fresh meadows, whitestone*, forest hills*, bayside*, college point, jackson heights,
        elmhurst*, rego park, maspeth, woodside*, sunnyside*, corona*, kew gardens*, richmond hill*, ozone park, howard beach,
        far rockaway, rockaway beach, dumbo, williamsburg*, bushwick, greenpoint*, park slope, downtown brooklyn, brooklyn navy yard,
        red hook*, sunset park*, bay ridge, midtown manhattan, midtown*, lower manhattan, financial district*, hudson yards, soho*,
        tribeca, chelsea*, flatiron, flatiron district, union square*, times square, battery park city, world trade center,
        rockefeller center, nomad, noho, upper east side, upper west side, east village, west village, greenwich village,
        gramercy, kips bay, morningside heights, washington heights, inwood*, roosevelt island, new york county, kings county,
        queens county, bronx county, richmond county, jfk airport, laguardia"""),
    "westchester": ("Westchester", "commute", "ny", """
        westchester, westchester county, yonkers, white plains, new rochelle, mount vernon*, mt vernon*, rye*, rye brook, port chester,
        purchase*, harrison*, west harrison, mamaroneck, larchmont, scarsdale, hartsdale, ardsley, dobbs ferry, irvington*, tarrytown,
        sleepy hollow, elmsford, valhalla, hawthorne*, armonk, north castle, pleasantville*, chappaqua, mount kisco, mt kisco,
        bedford*, bedford hills, katonah, ossining, briarcliff manor, croton on hudson, peekskill, yorktown heights, somers*,
        bronxville, tuckahoe, eastchester, pelham*, greenburgh, thornwood, mount pleasant*"""),
    "long_island": ("Long Island", "commute", "ny", """
        long island, nassau county, garden city*, mineola*, hempstead*, uniondale, east meadow, westbury*, old westbury, jericho*,
        syosset, woodbury*, hicksville, bethpage, plainview*, farmingdale, melville, lake success, great neck, manhasset,
        port washington*, roslyn*, glen cove, oyster bay, rockville centre, valley stream, lynbrook, long beach*, freeport*,
        baldwin*, oceanside*, massapequa, levittown*, new hyde park, floral park, elmont, franklin square, carle place,
        williston park, bellmore, merrick, seaford*, wantagh, huntington*, huntington station, deer park*, amityville, babylon*"""),
    "rockland": ("Rockland", "commute", "ny", """
        rockland county, nyack, west nyack, pearl river*, orangeburg*, nanuet, new city, suffern, spring valley*, blauvelt, tappan,
        piermont, stony point*, chestnut ridge*, montebello*, airmont"""),
    "hudson_nj": ("Hudson County NJ", "commute", "nj", """
        hudson county, jersey city, hoboken, weehawken, union city*, west new york, north bergen, secaucus, bayonne, kearny*,
        harrison*, guttenberg, east newark"""),
    "bergen_passaic": ("Bergen/Passaic NJ", "commute", "nj", """
        bergen county, passaic county, northern new jersey, north jersey, hackensack, paramus, fort lee*, englewood*, englewood cliffs,
        teaneck, ridgefield park, ridgefield*, little ferry, moonachie, carlstadt, east rutherford, rutherford, lyndhurst*,
        north arlington, lodi*, garfield*, saddle brook, rochelle park, maywood*, river edge, oradell, emerson*, westwood*,
        park ridge*, montvale, woodcliff lake, mahwah, ramsey*, allendale*, franklin lakes, wyckoff, ridgewood*, glen rock*,
        fair lawn, elmwood park*, hasbrouck heights, wood ridge, teterboro, edgewater*, cliffside park, fairview*,
        palisades park, leonia, bergenfield, dumont*, closter, cresskill, tenafly, norwood*, northvale, old tappan,
        upper saddle river, saddle river, paterson, clifton*, passaic, wayne*, totowa, little falls*, woodland park*, haledon"""),
    "essex_union": ("Essex/Union NJ", "commute", "nj", """
        essex county, union county, newark*, east orange, west orange, south orange, orange*, maplewood*, montclair*,
        upper montclair, bloomfield*, nutley, belleville*, glen ridge, verona*, cedar grove*, caldwell*, west caldwell,
        north caldwell, essex fells, roseland*, livingston*, millburn, short hills, irvington*, elizabeth*, linden*, roselle,
        roselle park, union*, kenilworth*, cranford, westfield*, garwood, springfield*, summit*, new providence, murray hill*,
        berkeley heights, mountainside, scotch plains, fanwood, clark*, rahway, hillside*, plainfield*, winfield"""),
    "morris": ("Morris County NJ", "commute", "nj", """
        morris county, morristown*, morris township, parsippany, parsippany troy hills, troy hills, florham park, madison*,
        chatham*, whippany, hanover*, east hanover, cedar knolls, morris plains, denville, boonton, mountain lakes, montville*,
        pine brook, lincoln park*, rockaway*, randolph*, mendham, dover*, wharton*, kinnelon, butler*, riverdale*, towaco,
        harding*"""),
    "somerset": ("Somerset NJ", "commute", "nj", """
        somerset county, warren*, basking ridge, bernards, bernardsville, bridgewater*, somerville*, bedminster, branchburg,
        raritan*, franklin township*, somerset*, watchung, green brook, north plainfield"""),
    "central_nj": ("Middlesex/Monmouth NJ", "commute", "nj", """
        middlesex county, monmouth county, iselin, woodbridge*, avenel, colonia, edison*, metuchen, piscataway, new brunswick*,
        east brunswick, north brunswick, south plainfield, perth amboy, sayreville, south amboy, carteret*, highland park*,
        dunellen, middlesex*, old bridge, matawan, holmdel, red bank*, middletown*, hazlet, keyport, aberdeen*, little silver,
        eatontown, tinton falls, shrewsbury*, lincroft, rumson, atlantic highlands"""),
    "fairfield_ct": ("Fairfield County CT", "commute", "ct", """
        fairfield county, greenwich*, old greenwich, cos cob, riverside*, stamford, darien*, new canaan, norwalk*, south norwalk,
        westport*, wilton*, weston*, rowayton"""),
    # tier "stretch": off by default
    "fairfield_outer": ("Fairfield County CT (outer)", "stretch", "ct", """
        fairfield*, southport*, bridgeport*, stratford*, trumbull, shelton*, danbury*, bethel*, ridgefield*, newtown*, easton*,
        redding*, brookfield*, monroe*, milford*"""),
    "nj_outer": ("Outer NJ", "stretch", "nj", """
        princeton*, plainsboro, lawrenceville*, ewing*, trenton*, hamilton*, cranbury, south brunswick, monroe township*,
        freehold*, wall township, neptune*, long branch*, hillsborough*, chester*, long valley, budd lake, flanders*,
        mount olive*, succasunna, hackettstown, morganville"""),
    "ny_outer": ("Outer NY", "stretch", "ny", """
        suffolk county, hauppauge, islandia, ronkonkoma, bohemia, central islip, commack, smithtown, holbrook*, bay shore,
        brentwood*, patchogue, stony brook, putnam county, carmel*, brewster*, mahopac, orange county*, newburgh*, goshen*,
        middletown*"""),
}
METRO = """new york city metropolitan area, new york metropolitan area, new york city metro area, new york metro area, greater new york,
    greater new york city area, new york metro, nyc metro, nyc metro area, tri state, tristate, tri state area, new york area,
    new york city area, nyc area, metro new york"""

# 3-digit ZIP prefixes -> region (USPS sectional centers).
ZIP3 = {"100": "nyc", "101": "nyc", "102": "nyc", "103": "nyc", "104": "nyc", "111": "nyc", "112": "nyc", "113": "nyc", "114": "nyc",
        "116": "nyc", "105": "westchester", "106": "westchester", "107": "westchester", "108": "westchester", "109": "rockland",
        "110": "long_island", "115": "long_island", "117": "long_island", "118": "long_island", "119": "ny_outer",
        "070": "essex_union", "071": "essex_union", "072": "essex_union", "073": "hudson_nj", "074": "bergen_passaic",
        "075": "bergen_passaic", "076": "bergen_passaic", "077": "central_nj", "078": "morris", "079": "morris",
        "088": "central_nj", "089": "central_nj", "085": "nj_outer", "086": "nj_outer", "068": "fairfield_ct", "069": "fairfield_ct",
        "066": "fairfield_outer"}

STATES = {"al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado", "ct": "connecticut",
          "de": "delaware", "dc": "district of columbia", "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho", "il": "illinois",
          "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
          "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana",
          "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
          "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
          "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
          "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
          "pr": "puerto rico"}
STATE_OF = dict(STATES.items(), **{v: k for k, v in STATES.items()}, **{"washington dc": "dc", "washington d c": "dc", "d c": "dc"})
US = {"us", "usa", "u s", "u s a", "united states", "united states of america", "america", "north america", "nationwide", "usa wide",
      "us wide", "amer", "americas"}
FOREIGN = {"canada", "can", "ontario", "quebec", "british columbia", "alberta", "toronto", "toronto gta", "montreal", "vancouver",
           "ottawa", "calgary", "united kingdom", "uk", "gb", "england", "scotland", "london", "ireland", "dublin", "india", "bangalore",
           "bengaluru", "hyderabad", "pune", "mumbai", "germany", "berlin", "munich", "france", "paris", "spain", "madrid", "barcelona",
           "italy", "milan", "netherlands", "amsterdam", "poland", "warsaw", "krakow", "portugal", "lisbon", "sweden", "stockholm",
           "switzerland", "zurich", "israel", "tel aviv", "japan", "tokyo", "china", "shanghai", "beijing", "hong kong", "singapore",
           "australia", "sydney", "melbourne", "brazil", "sao paulo", "mexico city", "guadalajara", "monterrey", "colombia", "bogota", "argentina",
           "buenos aires", "philippines", "manila", "emea", "apac", "latam", "europe", "asia", "africa", "uae", "dubai", "teletravail",
           "south africa", "new zealand", "korea", "seoul", "taiwan", "vietnam", "romania", "ukraine", "czech republic", "prague",
           "denmark", "copenhagen", "norway", "oslo", "finland", "belgium", "brussels", "austria", "vienna", "luxembourg", "greece",
           "turkey", "istanbul", "egypt", "nigeria", "kenya", "pakistan", "costa rica", "chile", "peru", "guatemala", "malaysia",
           "indonesia", "thailand", "estonia", "lithuania", "latvia", "hungary", "serbia", "bulgaria", "croatia"}
ISO_FOREIGN = {"ca", "gb", "uk", "fr", "de", "in", "ie", "es", "it", "nl", "pl", "pt", "se", "ch", "il", "jp", "cn", "hk", "sg", "au",
               "br", "mx", "co", "ar", "ph", "ae", "za", "nz", "kr", "tw", "vn", "ro", "ua", "cz", "dk", "no", "fi", "be", "at", "lu",
               "gr", "tr", "eg", "ng", "ke", "pk", "cr", "cl", "pe", "my", "id", "th", "ee", "lt", "lv", "hu", "rs", "bg", "hr"}
CA_PROVINCES = {"on", "bc", "qc", "ab", "mb", "sk", "ns", "nb", "nl", "pe"}
REMOTE = re.compile(r"\b(remote|anywhere|work from home|wfh|telecommute|teletravail|virtual|distributed)\b", re.I)
MAYBE = re.compile(r"^\s*(\d+\s+locations?|multiple locations?|various( locations?)?|several locations|flexible|hybrid|on ?site|tbd|n/?a)\s*$", re.I)
SPLIT = re.compile(r"\s*(?:;|\||•|·|\s/\s|\s+or\s+|\n|\\n)\s*", re.I)

# ---- index: name -> [(region, state, ambiguous)] ----
_INDEX = {}
for _key, (_label, _tier, _state, _places) in REGIONS.items():
    for _p in _places.split(","):
        _p = _p.strip()
        if _p:
            _INDEX.setdefault(_p.rstrip("*"), []).append((_key, _state, _p.endswith("*")))
for _p in METRO.split(","):
    if _p.strip():
        _INDEX.setdefault(_p.strip(), []).append(("nyc", "ny", False))
_FOREIGN_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(f) for f in FOREIGN if f != "can"), key=len, reverse=True)) + r")\b")
_PLACE_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(n) for n in _INDEX), key=len, reverse=True)) + r")\b")


def _words(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower().replace("&", " and "))).strip()


def _tier_ok(region):
    return REGIONS[region][1] in ENABLED_TIERS


def _segment(seg_raw):
    """One place in a multi-location string -> (local regions [(region, city)], remote?, states, us?, foreign?, maybe?)."""
    raw_comps = [c for c in (x.strip() for x in seg_raw.split(",")) if c]
    states, us, foreign = set(), False, False
    comps = []
    for i, rc in enumerate(raw_comps):
        c = _words(rc)
        if not c:
            continue
        # the last component in lower case is an ISO country code ("Lyon, ..., fr"; "Stamford, CT, us")
        if i == len(raw_comps) - 1 and i > 0 and re.fullmatch(r"[a-z]{2}", rc):
            if rc == "us" or rc == "pr":
                us = True
            elif rc in ISO_FOREIGN:
                foreign = True
            continue
        if i == len(raw_comps) - 1 and i > 0 and c == "ca" and len(comps) and comps[-1][1] in CA_PROVINCES:
            foreign = True
            continue
        w = c.split()
        # peel leading/trailing country and state codes: "us ny new york", "ny new york", "yonkers ny", "ny us"
        changed = True
        while changed and len(w) > 1:
            changed = False
            for n in (3, 2, 1):
                head, tail = " ".join(w[:n]), " ".join(w[-n:])
                if len(w) > n and head in US:
                    us, w, changed = True, w[n:], True
                    break
                if len(w) > n and tail in US:
                    us, w, changed = True, w[:-n], True
                    break
                if len(w) > n and n > 1 and tail in STATE_OF and tail != "new york" and w[0] in ("remote", "hybrid"):
                    states.add(STATE_OF[tail])      # "Remote - New Mexico"
                    w, changed = w[:-n], True
                    break
                if len(w) > n and n == 1 and head in STATES and head != "in":
                    states.add(head)
                    w, changed = w[1:], True
                    break
                if len(w) > n and n == 1 and tail in STATES:
                    states.add(tail)
                    w, changed = w[:-1], True
                    break
        c = " ".join(w)
        if c in US:
            us = True
            continue
        if c in FOREIGN or c in ("remote canada", "canada remote"):
            foreign = True
            continue
        if i > 0 and (c in STATES or (c in STATE_OF and c != "new york")):
            states.add(STATE_OF[c] if c not in STATES else c)
            continue
        if i > 0 and c == "new york" and comps:
            states.add("ny")                      # "Albany, New York": the state
            continue
        comps.append((i, c))
    if any(_FOREIGN_RE.search(c) for _, c in comps):
        foreign = True
    remote = bool(REMOTE.search(seg_raw))
    found = []
    seg_text = _words(seg_raw)
    zm = re.search(r"(?:\b(?:ny|nj|ct)\b|new york|new jersey|connecticut)[\s,]+(\d{5})(?:-\d{4})?\b", seg_raw, re.I)
    if zm and zm.group(1)[:3] in ZIP3:
        found.append((ZIP3[zm.group(1)[:3]], ""))
    for i, c in comps:
        if c in STATE_OF and i == 0 and c != "new york":
            st = STATE_OF[c] if c not in STATES else c
            states.add(st)
            continue
        if c.startswith("new york") and not c.startswith("new york city") and not foreign:
            if c == "new york" or not (states - {"ny"}):
                found.append(("nyc", "new york"))
                continue
        for m in _PLACE_RE.finditer(c):
            name = m.group(1)
            if name == "new york":
                continue
            for region, st, amb in _INDEX[name]:
                if foreign:
                    continue
                if states and st not in states:
                    continue
                if not states and amb:
                    continue
                found.append((region, name))
    statewide = not found and not [c for _, c in comps if c not in STATE_OF] and bool(states & {"nj", "ct"})
    foreign = foreign and not us and not states            # "Ontario, CA" is California
    return found, remote, states, us, foreign, statewide


def resolve(loc, remote=False, text=""):
    p = _resolve(str(loc or "").strip(), bool(remote), frozenset(ENABLED_TIERS))
    if p.status == "maybe" and text:
        return _from_text(text, remote) or p
    return p


@functools.lru_cache(maxsize=20000)
def _resolve(raw, remote, _tiers):
    if not raw or MAYBE.match(raw):
        return Place("maybe", "", "", "")
    s = raw.replace("(", ",").replace(")", ",").replace("[", ",").replace("]", ",")
    best_local, remote_ok, bare_remote, remote_seen, maybe, any_foreign, any_us = None, False, False, False, False, False, False
    for seg in SPLIT.split(s):
        if not seg.strip(" ,"):
            continue
        if MAYBE.match(seg.strip(" ,")):
            maybe = True
            continue
        found, rem, states, us, foreign, statewide = _segment(seg)
        any_foreign |= foreign
        any_us |= us or bool(states)
        for region, city in found:
            if best_local is None or (_tier_ok(region) and not _tier_ok(best_local[0])) or (region == "nyc" and best_local[0] != "nyc" and _tier_ok(region)):
                best_local = (region, city)
        if statewide:
            maybe = True
        if rem:
            remote_seen = True
            if foreign:
                continue
            if states and not (states & HOME_STATES) and not us:
                continue                          # "Remote PA": limited to another state
            if us or states:
                remote_ok = True
            else:
                bare_remote = True                # plain "Remote": open unless every other place named is abroad
    if best_local and _tier_ok(best_local[0]):
        region, city = best_local
        return Place("local", region, REGIONS[region][1], city or region)
    abroad_only = any_foreign and not any_us
    if ALLOW_REMOTE_US and (remote_ok or (bare_remote and not abroad_only) or (remote and not remote_seen and not abroad_only)):
        return Place("remote_us", "remote", "remote", "remote")
    if best_local:
        return Place("out", best_local[0], REGIONS[best_local[0]][1], best_local[1])
    if maybe:
        return Place("maybe", "", "", "")
    return Place("out", "", "", "")


def _from_text(text, remote):
    """A blank or 'Multiple locations' job: the description often says where (\"based in our New York office\")."""
    if not text:
        return None
    t = str(text)[:6000]
    if re.search(r"\b(new york city|nyc|manhattan|brooklyn|new york, ny|jersey city|hoboken|stamford, ct|white plains|our new york office)\b", t, re.I):
        return Place("local", "nyc", "core", "new york")
    return None


def place_ok(loc, remote=False, text=""):
    return resolve(loc, remote, text).status in ("local", "remote_us")


def label(place):
    if place.status == "local":
        return REGIONS[place.region][0]
    if place.status == "remote_us":
        return "Remote (US)"
    if place.status == "out" and place.region:
        return REGIONS[place.region][0] + " (outside commute area)"
    return {"maybe": "Unclear", "out": "Outside area"}.get(place.status, "")


def loc_key(loc, remote=False):
    """Location part of a job's identity: all of NYC is one place; elsewhere the town; remote is remote."""
    p = resolve(loc, remote)
    if p.status == "local":
        return "nyc" if p.region == "nyc" else p.city
    if p.status == "remote_us":
        return "remote"
    return _words(str(loc or "").split(",")[0])[:40]


# The tracker's finder (Apps Script) still checks locations against its own short list. A row the resolver accepts but that
# list would drop gets a hint the finder recognizes, so the job reaches Leads before the finder itself is updated.
_FINDER_LOCAL = ["new york", "nyc", "manhattan", "brooklyn", "jersey city", "hoboken", "stamford"]
_FINDER_FOREIGN = ["canada", "london", "united kingdom", "india", "emea", "europe", "apac", "singapore", "dublin", "toronto",
                   "sydney", "paris", "berlin", "tokyo", "mexico", "brazil", "israel"]


def _finder_accepts(loc):
    l = str(loc or "").lower()
    if any(p in l for p in _FINDER_LOCAL):
        return True
    if "remote" in l or "anywhere" in l:
        return not any(p in l for p in _FINDER_FOREIGN) or bool(re.search(r"united states|usa|\bus\b|u\.s\.|north america", l))
    return False


def finder_location(loc, place):
    if place.status not in ("local", "remote_us") or _finder_accepts(loc):
        return loc
    hint = "New York metro: " + REGIONS[place.region][0] if place.status == "local" else "Remote, US"
    return f"{loc} ({hint})" if loc else hint
