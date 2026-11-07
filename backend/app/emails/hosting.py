"""Who hosts a business's email, from its mail servers (MX records, saved by the email check).

    mail_provider(["aspmx.l.google.com"]) -> "Google Workspace"

Says something about the business: Google Workspace or Microsoft 365 means an established
setup; a Gmail address means a very small business; a business email filter (Proofpoint,
Mimecast) means a bigger one with an IT person. It also tells how reliable the mailbox check
is (Microsoft 365 hides unknown mailboxes, see app/emails/smtp.py).
"""
from .syntax import is_free, split_email

PROVIDERS = {
    "google.com": "Google Workspace",
    "googlemail.com": "Google Workspace",
    "outlook.com": "Microsoft 365",
    "zoho.com": "Zoho Mail",
    "zoho.eu": "Zoho Mail",
    "zoho.in": "Zoho Mail",
    "secureserver.net": "GoDaddy email",
    "privateemail.com": "Namecheap email",
    "titan.email": "Titan email",
    "protonmail.ch": "Proton Mail",
    "yahoodns.net": "Yahoo business email",
    "icloud.com": "iCloud email",
    "mimecast.com": "Business email filter (Mimecast)",
    "pphosted.com": "Business email filter (Proofpoint)",
    "ppe-hosted.com": "Business email filter (Proofpoint)",
    "barracudanetworks.com": "Business email filter (Barracuda)",
    "messagelabs.com": "Business email filter (Symantec)",
    "hostinger.com": "Hostinger email",
    "ionos.com": "IONOS email",
    "1and1.com": "IONOS email",
    "emailsrvr.com": "Rackspace email",
}
OTHER = "Own or web-host mail server"
FREE = "Free email address (Gmail, Yahoo…)"


def provider_key(mx_host: str) -> str:
    """'acme-com.mail.protection.outlook.com' -> 'outlook.com'; 'mx.acme.co.uk' -> 'acme.co.uk'."""
    parts = mx_host.lower().rstrip(".").split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "ac", "gov", "edu"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def mail_provider(mx_hosts: list[str] | None) -> str | None:
    """A plain name for the email host, or None when the mail servers aren't known."""
    for host in mx_hosts or []:
        if host:
            return PROVIDERS.get(provider_key(host), OTHER)
    return None


def email_domain(email: str | None) -> str | None:
    """The domain to look up for an address, or None for a free address (gmail.com...)."""
    if not email or "@" not in email:
        return None
    return None if is_free(email.lower()) else split_email(email.strip().lower())[1]
