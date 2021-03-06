from flask import Flask, request
from flask_cors import CORS
from jinja2 import Environment, PackageLoader
from IPython.display import display, HTML
from interpret.utils.environment import EnvironmentDetector, is_cloud_env
import threading
import socket
import requests
import re
import os
import json
import atexit
from .explanation_dashboard_input import ExplanationDashboardInput
from ._internal.constants import DatabricksInterfaceConstants

try:
    from gevent.pywsgi import WSGIServer
except ModuleNotFoundError:
    raise RuntimeError("Error: gevent package is missing, please run 'conda install gevent' or"
                       "'pip install gevent' or 'pip install interpret-community[visualization]'")


class ExplanationDashboard:
    """Explanation Dashboard Class.

    :param explanation: An object that represents an explanation.
    :type explanation: ExplanationMixin
    :param model: An object that represents a model. It is assumed that for the classification case
        it has a method of predict_proba() returning the prediction probabilities for each
        class and for the regression case a method of predict() returning the prediction value.
    :type model: object
    :param dataset:  A matrix of feature vector examples (# examples x # features), the same samples
        used to build the explanation. Overwrites any existing dataset on the explanation object.
    :type dataset: numpy.array or list[][]
    :param true_y: The true labels for the provided dataset. Overwrites any existing dataset on the
        explanation object.
    :type true_y: numpy.array or list[]
    :param classes: The class names.
    :type classes: numpy.array or list[]
    :param features: Feature names.
    :type features: numpy.array or list[]
    :param port: The port to use on locally hosted service.
    :type port: int
    :param use_cdn: Whether to load latest dashboard script from cdn, fall back to local script if False.
    :type use_cdn: bool
    """

    service = None
    explanations = {}
    model_count = 0
    using_fallback = False
    _cdn_path = "v0.2.js"
    _dashboard_js = None
    env = Environment(loader=PackageLoader(__name__, 'templates'))
    default_template = env.get_template("inlineDashboard.html")

    class DashboardService:
        app = Flask(__name__)
        CORS(app)

        def __init__(self, port):
            self.port = port
            self.ip = 'localhost'
            self.env = "local"
            self.use_cdn = True
            if self.port is None:
                # Try 100 different ports
                for port in range(5000, 5100):
                    available = ExplanationDashboard.DashboardService._local_port_available(self.ip, port, rais=False)
                    if available:
                        self.port = port
                        return
                error_message = """Ports 5000 to 5100 not available.
                    Please specify an open port for use via the 'port' parameter"""
                raise RuntimeError(
                    error_message.format(port)
                )
            else:
                ExplanationDashboard.DashboardService._local_port_available(self.ip, self.port)

        def run(self):
            class devnull:
                write = lambda _: None  # noqa: E731

            server = WSGIServer((self.ip, self.port), self.app, log=devnull)
            self.app.config["server"] = server
            server.serve_forever()

            # Closes server on program exit, including freeing all sockets
            def closeserver():
                server.stop()

            atexit.register(closeserver)

        def get_base_url(self):
            env = EnvironmentDetector()
            detected_envs = env.detect()
            in_cloud_env = is_cloud_env(detected_envs)
            # First handle known cloud environments
            nbvm_file_path = "/mnt/azmnt/.nbvm"
            if not (os.path.exists(nbvm_file_path) and os.path.isfile(nbvm_file_path)):
                if not in_cloud_env:
                    return "http://{0}:{1}".format(
                        self.ip,
                        self.port)
                # all non-specified cloud environments are not handled
                self.env = "cloud"
                return None
            self.env = "cloud"
            # regex to find items of the form key=value where value will be part of a url
            # the keys of interest to us are "instance" and domainsuffix"
            envre = re.compile(r'''^([^\s=]+)=(?:[\s"']*)(.+?)(?:[\s"']*)$''')
            result = {}
            with open(nbvm_file_path) as nbvm_variables:
                for line in nbvm_variables:
                    match = envre.match(line)
                    if match is not None:
                        result[match.group(1)] = match.group(2)

            if "instance" not in result or "domainsuffix" not in result:
                return None
            self.env = "azure"
            instance_name = result["instance"]
            domain_suffix = result["domainsuffix"]
            return "https://{}-{}.{}".format(instance_name, self.port, domain_suffix)

        @staticmethod
        def _local_port_available(ip, port, rais=True):
            """
            Borrowed from:
            https://stackoverflow.com/questions/19196105/how-to-check-if-a-network-port-is-open-on-linux
            """
            try:
                backlog = 5
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.bind((ip, port))
                sock.listen(backlog)
                sock.close()
            except socket.error:  # pragma: no cover
                if rais:
                    error_message = """Port {0} is not available.
                    Please specify another port for use via the 'port' parameter"""
                    raise RuntimeError(
                        error_message.format(port)
                    )
                else:
                    return False
            return True

        @app.route('/')
        def hello():
            return "No global list view supported at this time."

        @app.route('/<id>')
        def explanation_visual(id):
            if id in ExplanationDashboard.explanations:
                return generate_inline_html(ExplanationDashboard.explanations[id], None)
            else:
                return "Unknown model id."

        @app.route('/<id>/predict', methods=['POST'])
        def predict(id):
            data = request.get_json(force=True)
            if id in ExplanationDashboard.explanations:
                return ExplanationDashboard.explanations[id].on_predict(data)

    def __init__(self, explanation, model=None, *, dataset=None,
                 true_y=None, classes=None, features=None, port=None, use_cdn=True,
                 datasetX=None, trueY=None, locale=None):
        # support legacy kwarg names
        if dataset is None and datasetX is not None:
            dataset = datasetX
        if true_y is None and trueY is not None:
            true_y = trueY
        self._initialize_js(use_cdn)
        predict_url = None
        local_url = None
        if not ExplanationDashboard.service:
            try:
                ExplanationDashboard.service = ExplanationDashboard.DashboardService(port)
                self._thread = threading.Thread(target=ExplanationDashboard.service.run, daemon=True)
                self._thread.start()
            except Exception as e:
                ExplanationDashboard.service = None
                raise e
        ExplanationDashboard.service.use_cdn = use_cdn
        ExplanationDashboard.model_count += 1
        base_url = ExplanationDashboard.service.get_base_url()
        if base_url is not None:
            predict_url = "{0}/{1}/predict".format(
                base_url,
                str(ExplanationDashboard.model_count))
            local_url = "{0}/{1}".format(
                base_url,
                str(ExplanationDashboard.model_count))
        explanation_input =\
            ExplanationDashboardInput(explanation, model, dataset, true_y, classes, features, predict_url, locale)
        # Due to auth, predict is only available in separate tab in cloud after login
        if ExplanationDashboard.service.env == "local":
            explanation_input.enable_predict_url()
        html = generate_inline_html(explanation_input, local_url)
        if ExplanationDashboard.service.env == "azure":
            explanation_input.enable_predict_url()

        ExplanationDashboard.explanations[str(ExplanationDashboard.model_count)] = explanation_input

        if "DATABRICKS_RUNTIME_VERSION" in os.environ:
            _render_databricks(html)
        else:
            display(HTML(html))

    def _initialize_js(self, use_cdn):
        if (ExplanationDashboard._dashboard_js is None):
            if (use_cdn):
                try:
                    url = 'https://interpret-cdn.azureedge.net/{0}'.format(ExplanationDashboard._cdn_path)
                    r = requests.get(url)
                    if not r.ok:
                        ExplanationDashboard.using_fallback = True
                        self._load_local_js()
                    r.encoding = "utf-8"
                    ExplanationDashboard._dashboard_js = r.text
                except Exception:
                    ExplanationDashboard.using_fallback = True
                    self._load_local_js()
            else:
                self._load_local_js()

    def _load_local_js(self):
        script_path = os.path.dirname(os.path.abspath(__file__))
        js_path = os.path.join(script_path, "static", "index.js")
        with open(js_path, "r", encoding="utf-8") as f:
            ExplanationDashboard._dashboard_js = f.read()


def generate_inline_html(explanation_input_object, local_url):
    explanation_input = json.dumps(explanation_input_object.dashboard_input)
    return ExplanationDashboard.default_template.render(explanation=explanation_input,
                                                        main_js=ExplanationDashboard._dashboard_js,
                                                        app_id='app_123',
                                                        using_fallback=ExplanationDashboard.using_fallback,
                                                        local_url=local_url,
                                                        has_local_url=local_url is not None)


# NOTE: Code mostly derived from Plotly's databricks render as linked below:
# https://github.com/plotly/plotly.py/blob/01a78d3fdac14848affcd33ddc4f9ec72d475232/packages/python/plotly/plotly/io/_base_renderers.py
def _render_databricks(html):  # pragma: no cover
    import inspect

    if _render_databricks.displayHTML is None:
        found = False
        for frame in inspect.getouterframes(inspect.currentframe()):
            global_names = set(frame.frame.f_globals)
            target_names = {DatabricksInterfaceConstants.DISPLAY_HTML,
                            DatabricksInterfaceConstants.DISPLAY,
                            DatabricksInterfaceConstants.SPARK}
            if target_names.issubset(global_names):
                _render_databricks.displayHTML = frame.frame.f_globals[
                    DatabricksInterfaceConstants.DISPLAY_HTML]
                found = True
                break

        if not found:
            msg = "Could not find databrick's displayHTML function"
            raise RuntimeError(msg)

    _render_databricks.displayHTML(html)


_render_databricks.displayHTML = None
