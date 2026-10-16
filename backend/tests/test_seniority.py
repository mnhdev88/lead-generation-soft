"""Job title -> seniority / department / decision-maker (app/people/seniority.py)."""
import pytest

from app.people import SENIORITY_RANK, classify

# (title, seniority, department, is_decision_maker)
CASES = [
    # owners
    ("Owner • DDS • DMD", "owner", "management", True),
    ("Owner", "owner", "management", True),
    ("Founder & CEO", "owner", "management", True),
    ("CEO & Co-Founder", "owner", "management", True),
    ("Co-Founder", "owner", "management", True),
    ("Cofounder", "owner", "management", True),
    ("Managing Partner", "owner", "management", True),
    ("Partner", "owner", "management", True),
    ("Founding Attorney", "owner", "management", True),
    ("Principal Dentist", "owner", "management", True),
    ("Principal", "owner", "management", True),
    ("Broker/Owner", "owner", "management", True),
    ("Proprietor", "owner", "management", True),
    ("Dentist & Owner", "owner", "management", True),
    # C-level
    ("CEO", "c_level", "management", True),
    ("President", "c_level", "management", True),
    ("Chief Medical Officer", "c_level", "medical", True),
    ("CFO", "c_level", "finance", True),
    ("Chief Technology Officer", "c_level", "tech", True),
    ("COO", "c_level", "operations", True),
    ("Managing Director", "c_level", "management", True),
    ("Executive Director", "c_level", "management", True),
    ("Chairman of the Board", "c_level", "management", True),
    # directors
    ("Head of Sales", "director", "sales", True),
    ("VP Marketing", "director", "marketing", True),
    ("Vice President of Sales", "director", "sales", True),
    ("Vice-President, Finance", "director", "finance", True),
    ("Director of Operations", "director", "operations", True),
    ("Medical Director", "director", "medical", True),
    ("Practice Director", "director", "management", True),
    # managers
    ("Office Manager", "manager", "admin", True),
    ("Practice Administrator", "manager", "management", True),
    ("Practice Manager", "manager", "management", True),
    ("General Manager", "manager", "management", True),
    ("Operations Manager", "manager", "operations", True),
    ("Managing Broker", "manager", "sales", True),
    ("Marketing Manager", "manager", "marketing", False),
    ("Assistant Manager", "manager", "admin", False),
    ("Assistant Office Manager", "manager", "admin", False),
    ("Assistant Director of Nursing", "manager", "medical", False),
    ("Team Lead", "manager", "other", False),
    ("Supervisor", "manager", "other", False),
    ("Product Owner", "manager", "other", False),
    # professionals
    ("Dr.", "professional", "medical", False),
    ("General Dentist", "professional", "medical", False),
    ("Associate Dentist", "professional", "medical", False),
    ("Hygienist", "professional", "medical", False),
    ("Registered Dental Hygienist", "professional", "medical", False),
    ("Lead Hygienist", "professional", "medical", True),
    ("Attorney", "professional", "legal", False),
    ("Realtor", "professional", "sales", False),
    ("Real Estate Agent", "professional", "sales", False),
    ("Principal Engineer", "professional", "tech", True),
    ("Software Engineer", "professional", "tech", False),
    ("Accountant", "professional", "finance", False),
    ("Orthodontist", "professional", "medical", False),
    # staff
    ("Front Desk Coordinator", "staff", "admin", False),
    ("Receptionist", "staff", "admin", False),
    ("Dental Assistant", "staff", "medical", False),
    ("Chair-side Assistant", "staff", "medical", False),
    ("Executive Assistant to the CEO", "staff", "admin", False),
    ("Owner's Assistant", "staff", "admin", False),
    ("Treatment Coordinator", "staff", "sales", False),
    ("Sales Associate", "staff", "sales", False),
    ("Billing Specialist", "staff", "finance", False),
    ("Intern", "staff", "other", False),
    ("Paralegal", "staff", "legal", False),
]


@pytest.mark.parametrize("title, seniority, department, decision_maker", CASES)
def test_classify(title, seniority, department, decision_maker):
    result = classify(title)
    assert (result.seniority, result.department, result.is_decision_maker) == (seniority, department, decision_maker)


@pytest.mark.parametrize("title", [None, "", "   ", "•"])
def test_empty_title(title):
    result = classify(title)
    assert result.seniority is None and result.department is None and result.is_decision_maker is False


def test_word_boundaries_avoid_false_matches():
    # "cto" inside "Director", "owner" inside "Downer", "partner" in "Partnerships"
    assert classify("Director").seniority == "director"
    assert classify("Mr Downer").seniority is None
    assert classify("Partnerships Coordinator").seniority == "staff"
    assert classify("Channel Partner Manager").seniority == "manager"


def test_unknown_title_has_other_department():
    result = classify("Team Member of the Month")
    assert result.department == "other" and result.is_decision_maker is False


def test_rank_order():
    order = ["owner", "c_level", "director", "manager", "professional", "staff", None]
    assert sorted(order, key=SENIORITY_RANK.get) == order
