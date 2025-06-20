# ScholarDrop

*Drop a PDF. Deposit a publication.*

ScholarDrop is a standalone Python web application for submitting publications to
SHAREOK (and, in the future, any DSpace 7+ instance). A user picks a repository,
signs in with their repository credentials, chooses a collection they are
authorized to submit to, and uploads a single PDF. ScholarDrop extracts the title,
authors, committee members, keywords, and abstract from the PDF, pre-fills the
collection's submission form, lets the user correct or add anything the extraction
missed, and — only after explicit approval — deposits the item through the DSpace
REST API.
