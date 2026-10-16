"""Turn a messy job title from a website into seniority, department and "decision-maker".

Examples:
    "Owner • DDS • DMD"        -> owner,        management, decision-maker
    "Chief Medical Officer"    -> c_level,      medical,    decision-maker
    "Office Manager"           -> manager,      admin,      decision-maker
    "Assistant Manager"        -> manager,      other,      not a decision-maker
    "Hygienist"                -> professional, medical
    "Front Desk Coordinator"   -> staff,        admin

How it works: the title is lower-cased and punctuation becomes spaces ("Co-Founder" ->
"co founder"), then keyword rules are tried from most to least senior and the first
match wins. All rules use word boundaries (\\b), so "cto" never matches "director".
"""
import re
from dataclasses import dataclass

# Lower number = more senior. Used to sort people (most senior first).
SENIORITY_RANK: dict[str | None, int] = {
    "owner": 0,
    "c_level": 1,
    "director": 2,
    "manager": 3,
    "professional": 4,
    "staff": 5,
    None: 9,
}


@dataclass
class Classification:
    seniority: str | None  # owner | c_level | director | manager | professional | staff | None
    department: str | None  # management | sales | marketing | operations | finance | medical | legal | tech | admin | other
    is_decision_maker: bool


def _rx(*patterns: str) -> re.Pattern:
    """One regex that matches any of the patterns as whole words."""
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b")


# --- seniority rules -----------------------------------------------------------------

# Assistants *to* somebody senior: "Executive Assistant to the CEO", "Owner's Assistant".
# Checked first so the senior word inside the title doesn't win.
_ASSISTANT_TO = _rx(
    r"(?:executive|personal|administrative|admin|office|dental|medical|legal|virtual|chair ?side) assistant",
    r"assistant to", r"s assistant", r"secretary", r"receptionist", r"chair ?side",
)

# "Assistant Director", "Deputy Director", "Associate Director": a manager-level role.
_JUNIOR_DIRECTOR = _rx(r"(?:assistant|asst|associate|deputy) (?:director|vice president|vp)")

_OWNER = _rx(
    r"(?<!product )(?<!process )owner", r"co ?owner", r"owner operator",
    r"founder", r"co ?founder", r"founding", r"proprietor",
    r"managing (?:partner|member)",
    # "Partner" but not "Partner Manager" / "Partner Relations" (those are about business partners)
    r"partner(?! (?:manager|relations|success|program|marketing|development|account))",
    # "Principal Dentist", "Principal Broker", "Principal" - but not "Principal Engineer" (a senior expert)
    r"principal(?! (?:engineer|software|scientist|consultant|developer|designer|analyst|product|program|data|architect|investigator))",
)
_C_LEVEL = _rx(
    r"ceo", r"coo", r"cfo", r"cto", r"cmo", r"cio", r"cro", r"cpo", r"cco", r"ciso", r"cso", r"cdo",
    r"chief", r"(?<!vice )president", r"managing director", r"executive director",
    r"chair(?:man|woman|person)?", r"general partner",
)
_DIRECTOR = _rx(
    r"director", r"head of", r"(?:department|team|division) head", r"vp", r"svp", r"evp", r"avp", r"vice president", r"dean",
)
_MANAGER = _rx(
    r"manager", r"mgr", r"supervisor", r"superintendent", r"foreman",
    # "Practice Administrator" - but not "System Administrator" (an IT job)
    r"(?<!system )(?<!systems )(?<!network )(?<!database )(?<!it )(?<!web )administrator",
    r"(?:team|tech|technical|project|shift|sales|marketing|design|operations|office) lead",
    r"managing broker", r"controller", r"product owner",
)
_PROFESSIONAL = _rx(
    # medical / dental
    r"dentist", r"doctor", r"dr", r"physician", r"surgeon", r"orthodontist", r"periodontist",
    r"endodontist", r"prosthodontist", r"pediatric dentist", r"hygienist", r"nurse", r"rn",
    r"np", r"pa c", r"therapist", r"pharmacist", r"veterinarian", r"dvm", r"optometrist",
    r"chiropractor", r"dds", r"dmd", r"md", r"bds", r"mbbs", r"physiotherapist", r"psychologist",
    r"dietitian", r"nutritionist", r"radiologist", r"anesthesiologist", r"dermatologist",
    # legal / finance
    r"attorney", r"lawyer", r"solicitor", r"barrister", r"advocate", r"counsel", r"esq",
    r"accountant", r"cpa", r"auditor", r"actuary",
    # technical / creative
    r"(?:system|systems|network|database|it|web) administrator",
    r"engineer", r"developer", r"programmer", r"architect", r"designer", r"scientist", r"analyst",
    # real estate / advisory
    r"realtor", r"agent", r"broker", r"consultant", r"advisor", r"adviser", r"planner",
)
_STAFF = _rx(
    r"assistant", r"asst", r"receptionist", r"front desk", r"front office", r"coordinator",
    r"associate", r"intern", r"technician", r"tech", r"clerk", r"secretary", r"scheduler",
    r"representative", r"rep", r"trainee", r"apprentice", r"cashier", r"specialist",
    r"account executive", r"team member", r"staff", r"bookkeeper", r"paralegal", r"officer",
    r"concierge", r"biller",
)

# Managers who usually decide for a small business ("Office Manager" at a dental clinic).
_DECIDING_MANAGER = _rx(
    r"(?:office|practice|general|operations|clinic|branch|store|restaurant) (?:manager|administrator)",
    r"managing broker",
)
_JUNIOR = _rx(r"assistant", r"asst", r"deputy", r"associate")
_SENIOR_PROFESSIONAL = _rx(r"principal", r"lead", r"owner", r"senior partner")

# --- department rules (first match wins) -----------------------------------------------

# Specific C-suite roles name their own department.
_DEPT_C_SUITE: list[tuple[str, re.Pattern]] = [
    ("finance", _rx(r"cfo", r"chief financial", r"chief accounting")),
    ("tech", _rx(r"cto", r"cio", r"ciso", r"cdo", r"chief (?:technology|technical|information|digital|data)")),
    ("marketing", _rx(r"cmo", r"chief marketing", r"chief brand")),
    ("operations", _rx(r"coo", r"chief operating", r"chief operations")),
    ("sales", _rx(r"cro", r"chief revenue", r"chief sales", r"chief commercial")),
    ("medical", _rx(r"chief medical", r"chief clinical", r"chief dental", r"chief nursing", r"medical director")),
    ("legal", _rx(r"chief legal", r"general counsel")),
]
_DEPT_MANAGEMENT = _rx(
    r"general manager", r"practice (?:manager|administrator|director)", r"managing director",
    r"executive director",
)
_DEPARTMENTS: list[tuple[str, re.Pattern]] = [
    ("medical", _rx(
        r"dentist", r"dental", r"doctor", r"dr", r"physician", r"surgeon", r"orthodont\w*", r"periodont\w*",
        r"endodont\w*", r"prosthodont\w*", r"hygien\w*", r"nurs\w*", r"medical", r"clinical", r"clinic",
        r"therap\w*", r"dds", r"dmd", r"md", r"bds", r"mbbs", r"rn", r"pharmac\w*", r"veterinar\w*", r"dvm",
        r"optometr\w*", r"chiropract\w*", r"patient", r"radiolog\w*", r"physio\w*", r"psycholog\w*",
        r"dermatolog\w*", r"anesthes\w*", r"health", r"chair ?side",
    )),
    ("legal", _rx(r"attorney", r"lawyer", r"legal", r"counsel", r"paralegal", r"solicitor", r"barrister",
                  r"advocate", r"law", r"esq", r"litigation", r"compliance")),
    ("finance", _rx(r"finance", r"financial", r"accountant", r"accounting", r"accounts", r"bookkeep\w*",
                    r"controller", r"treasurer", r"billing", r"biller", r"cpa", r"payroll", r"audit\w*",
                    r"tax", r"actuary", r"investment", r"insurance")),
    ("tech", _rx(r"engineer\w*", r"developer", r"software", r"it", r"technology", r"technical", r"data",
                 r"systems?", r"network", r"devops", r"web", r"programmer", r"cyber\w*", r"security")),
    ("marketing", _rx(r"marketing", r"brand", r"seo", r"social media", r"content", r"growth",
                      r"communications", r"pr", r"public relations", r"creative", r"designer", r"design")),
    ("sales", _rx(r"sales", r"business development", r"bd", r"account executive", r"account manager",
                  r"realtor", r"real estate", r"broker", r"agent", r"leasing", r"revenue", r"commercial",
                  r"partnerships?", r"treatment coordinator", r"new patient")),
    ("operations", _rx(r"operations", r"operating", r"logistics", r"supply chain", r"facilities",
                       r"production", r"warehouse", r"purchasing", r"procurement")),
    ("admin", _rx(r"receptionist", r"front desk", r"front office", r"office", r"administrative",
                  r"administrator", r"admin", r"secretary", r"scheduler", r"assistant", r"hr",
                  r"human resources", r"recruit\w*", r"people", r"talent", r"concierge")),
]


def _normalize(title: str) -> str:
    """'Co-Founder & CEO' -> 'co founder ceo'"""
    text = title.lower().replace("’", "'").replace("'s ", " s ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _seniority(t: str) -> str | None:
    if _ASSISTANT_TO.search(t):
        return "staff"
    if _JUNIOR_DIRECTOR.search(t):
        return "manager"
    for level, pattern in (
        ("owner", _OWNER), ("c_level", _C_LEVEL), ("director", _DIRECTOR),
        ("manager", _MANAGER), ("professional", _PROFESSIONAL), ("staff", _STAFF),
    ):
        if pattern.search(t):
            return level
    return None


def _department(t: str, seniority: str | None) -> str:
    for dept, pattern in _DEPT_C_SUITE:
        if pattern.search(t):
            return dept
    if seniority in ("owner", "c_level") or _DEPT_MANAGEMENT.search(t):
        return "management"
    for dept, pattern in _DEPARTMENTS:
        if pattern.search(t):
            return dept
    return "other"


def _decision_maker(t: str, seniority: str | None) -> bool:
    if seniority in ("owner", "c_level", "director"):
        return True
    if seniority == "manager":
        return bool(_DECIDING_MANAGER.search(t)) and not _JUNIOR.search(t)
    if seniority == "professional":
        return bool(_SENIOR_PROFESSIONAL.search(t))
    return False


def classify(title: str | None) -> Classification:
    """Classify a job title. Empty/unknown titles give seniority None."""
    t = _normalize(title or "")
    if not t:
        return Classification(seniority=None, department=None, is_decision_maker=False)
    seniority = _seniority(t)
    return Classification(
        seniority=seniority,
        department=_department(t, seniority),
        is_decision_maker=_decision_maker(t, seniority),
    )
