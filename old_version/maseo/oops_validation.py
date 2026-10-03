from pathlib import Path
from typing import Union
 
import requests
from bs4 import BeautifulSoup
from rdflib import Graph
 
class OOPSUnreadableError(RuntimeError):
    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message)
        self.raw_response = raw_response 
    
    
    
def _is_oops_error_response(soup: BeautifulSoup) -> bool:

    for desc in soup.find_all("rdf:Description"):
        about = desc.get("rdf:about") or ""
        if "oops/unexpected_error" in about:
            return True
    # Fallback: well-known error title
    for title in soup.find_all("oops:hasTitle"):
        if title.text and "something went wrong" in title.text.lower():
            return True
    return False
 

class OOPSValidation:

 
    def __init__(
        self,
        onto_str: Union[str, Path, Graph],
        api_url: str,
        request_template_path: Union[str, Path],
    ):
        self.ontology = onto_str
        self.oops_api = api_url
 
        template_path = Path(request_template_path)
        if not template_path.exists():
            raise FileNotFoundError(
                f"OOPS request template not found: {template_path}. "
                f"Check `oops.request_template` in config.yaml."
            )
        with open(template_path, "r", encoding="utf-8") as f:
            self.request_template = f.read()
 
        self.request_body = self._compose_request()
 
    def _compose_request(self, output_format: str = "XML", pitfalls: str = "") -> str:
        formatted_ontology = f"<![CDATA[ {self.ontology} ]]></OntologyContent>"
        formatted_request = self.request_template.replace("</OntologyContent>", formatted_ontology)
 
        if output_format not in ["XML", "RDF/XML"]:
            raise ValueError(f"Invalid output format: {output_format}")
        formatted_output = f"{output_format}</OutputFormat>"
        formatted_request = formatted_request.replace("</OutputFormat>", formatted_output)
 
        if pitfalls:
            formatted_pitfalls = f"{pitfalls}</Pitfalls>"
            formatted_request = formatted_request.replace("</Pitfalls>", formatted_pitfalls)
 
        return formatted_request
 
    def validate(self) -> str:
        try:
            response = requests.post(
                url=self.oops_api,
                data=self.request_body,
                allow_redirects=False,
            ).text
            response = BeautifulSoup(response, features="xml").prettify()
            return response
        except Exception as e:
            raise Exception(f"Error connecting to the OOPS! API: {e}") from e
 
 
def format_oops_nl(oops_response: str) -> str:
    soup = BeautifulSoup(oops_response, "xml")
 
    if _is_oops_error_response(soup):
        # Pull the message for logging if present
        msg_tag = soup.find("oops:hasMessage")
        msg = msg_tag.text.strip() if msg_tag and msg_tag.text else "OOPS! returned unexpected_error"
        raise OOPSUnreadableError(msg)
 
    oops_prompt = "Here is a list of oops pitfalls: \n"
    pitfalls = soup.find_all("oops:Pitfall")
    for pitfall in pitfalls:
        description_tags = pitfall.find_all("oops:Description")
        description = description_tags[0].text if description_tags else ""
        affects_tags = pitfall.find_all("oops:Affects")
        affects = affects_tags[0].text if affects_tags else ""
        oops_prompt += description
        oops_prompt += affects
    return oops_prompt