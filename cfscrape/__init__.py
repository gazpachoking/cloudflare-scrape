import time
import re
import os
from requests.sessions import Session
import execjs

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

DEFAULT_USER_AGENT = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:39.0) Gecko/20100101 Firefox/39.0"


def _find_no_duplicates(cj, name, domain=None, path=None):
    result = None
    for cookie in cj:
        if cookie.name == name:
            if domain is None or cookie.domain == domain:
                if path is None or cookie.path == path:
                    if result is not None:  # if there are multiple cookies that meet passed in criteria
                        raise KeyError("There are multiple cookies with name %r" % name)
                    result = cookie.value  # we will eventually return this as long as no cookie conflict

    return result


class CloudflareScraper():
    def __init__(self, *args, **kwargs):
        self.js_engine = kwargs.pop("js_engine", None)
        self.sess = kwargs.pop("sess", None)
        if not self.sess:
            self.sess = Session(*args, **kwargs)
        elif args or kwargs:
            # You shouldn't pass a session and session init params

        if "requests" in self.headers["User-Agent"]:
            # Spoof Firefox on Linux if no custom User-Agent has been set
            self.headers["User-Agent"] = DEFAULT_USER_AGENT

    def request(self, method, url, *args, **kwargs):
        resp = self.sess.request(method, url, *args, **kwargs)
        domain = url.split("/")[2]

        # Check if we already solved a challenge
        if _find_no_duplicates(self.sess.cookies, "cf_clearance", domain="." + domain):
            return resp

        # Check if Cloudflare anti-bot is on
        if ( "URL=/cdn-cgi/" in resp.headers.get("Refresh", "") and
             resp.headers.get("Server", "") == "cloudflare-nginx" ):
            return self.solve_cf_challenge(resp, **kwargs)

        # Otherwise, no Cloudflare anti-bot detected
        return resp

    def solve_cf_challenge(self, resp, **kwargs):
        time.sleep(5)  # Cloudflare requires a delay before solving the challenge

        body = resp.text
        parsed_url = urlparse(resp.url)
        domain = urlparse(resp.url).netloc
        submit_url = "%s://%s/cdn-cgi/l/chk_jschl" % (parsed_url.scheme, domain)

        params = kwargs.setdefault("params", {})
        headers = kwargs.setdefault("headers", {})
        headers["Referer"] = resp.url

        try:
            params["jschl_vc"] = re.search(r'name="jschl_vc" value="(\w+)"', body).group(1)
            params["pass"] = re.search(r'name="pass" value="(.+?)"', body).group(1)

            # Extract the arithmetic operation
            js = self.extract_js(body)

        except Exception:
            # Something is wrong with the page.
            # This may indicate Cloudflare has changed their anti-bot
            # technique. If you see this and are running the latest version,
            # please open a GitHub issue so I can update the code accordingly.
            print("[!] Unable to parse Cloudflare anti-bots page. "
                  "Try upgrading cloudflare-scrape, or submit a bug report "
                  "if you are running the latest version. Please read "
                  "https://github.com/Anorov/cloudflare-scrape#updates "
                  "before submitting a bug report.\n")
            raise

        # Safely evaluate the Javascript expression
        params["jschl_answer"] = str(int(execjs.exec_(js)) + len(domain))

        return self.sess.get(submit_url, **kwargs)

    def extract_js(self, body):
        js = re.search(r"setTimeout\(function\(\){\s+(var "
                        "t,r,a,f.+?\r?\n[\s\S]+?a\.value =.+?)\r?\n", body).group(1)
        js = re.sub(r"a\.value =(.+?) \+ .+?;", r"\1", js)
        js = re.sub(r"\s{3,}[a-z](?: = |\.).+", "", js)

        # Strip characters that could be used to exit the string context
        # These characters are not currently used in Cloudflare's arithmetic snippet
        js = re.sub(r"[\n\\']", "", js)

        if "Node" in self.js_engine:
            # Use vm.runInNewContext to safely evaluate code
            # The sandboxed code cannot use the Node.js standard library
            return "return require('vm').runInNewContext('%s');" % js

        return js.replace("parseInt", "return parseInt")

def create_scraper(sess=None, js_engine=None):
    """
    Convenience function for creating a ready-to-go requests.Session (subclass) object.
    """
    if js_engine:
        os.environ["EXECJS_RUNTIME"] = js_engine

    js_engine = execjs.get().name

    if not ("Node" in js_engine or "V8" in js_engine):
        raise EnvironmentError("Your Javascript runtime '%s' is not supported due to security concerns. "
                               "Please use Node.js, V8, or PyV8. To use a specific engine, "
                               "such as Node, call create_scraper(js_engine=\"Node\")" % js_engine)

    scraper = CloudflareScraper(sess=sess, js_engine=js_engine)

    return scraper


## Functions for integrating cloudflare-scrape with other applications and scripts

def get_tokens(url, user_agent=None, js_engine=None):
    scraper = create_scraper(js_engine=js_engine)
    user_agent = user_agent or scraper.headers["User-Agent"]

    try:
        resp = scraper.get(url)
        resp.raise_for_status()
    except Exception as e:
        print("'%s' returned an error. Could not collect tokens.\n" % url)
        raise

    domain = urlparse(resp.url).netloc

    return ({
                "__cfduid": scraper.cookies.get("__cfduid", "", domain="." + domain),
                "cf_clearance": scraper.cookies.get("cf_clearance", "", domain="." + domain)
            },
            user_agent
           )

def get_cookie_string(url, user_agent=None, js_engine=None):
    """
    Convenience function for building a Cookie HTTP header value.
    """
    tokens, user_agent = get_tokens(url, user_agent=user_agent, js_engine=None)
    return "; ".join("=".join(pair) for pair in tokens.items()), user_agent
