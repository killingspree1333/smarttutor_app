# SmartTutor Desktop — API клиент
import requests
from PyQt6.QtCore import QThread, pyqtSignal

API_URL = "https://smarttutorapp-production.up.railway.app"

class ApiWorker(QThread):
    success = pyqtSignal(object)
    failure = pyqtSignal(str)

    def __init__(self, method, endpoint, token=None, data=None):
        super().__init__()
        self.method = method
        self.endpoint = endpoint
        self.token = token
        self.data = data

    def run(self):
        try:
            url = f"{API_URL}{self.endpoint}"
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            if self.method == "GET":
                r = requests.get(url, headers=headers, timeout=30)
            elif self.method == "POST":
                r = requests.post(url, headers=headers, json=self.data, timeout=30)
            elif self.method == "PATCH":
                r = requests.patch(url, headers=headers, json=self.data, timeout=30)
            elif self.method == "PUT":
                r = requests.put(url, headers=headers, json=self.data, timeout=30)
            elif self.method == "DELETE":
                r = requests.delete(url, headers=headers, timeout=30)
                self.success.emit({})
                return

            if r.status_code >= 400:
                try:
                    detail = r.json().get("detail", "Ошибка сервера")
                except:
                    detail = "Ошибка сервера"
                self.failure.emit(detail)
                return

            self.success.emit(r.json())
        except requests.exceptions.ConnectionError:
            self.failure.emit("Нет подключения к серверу")
        except requests.exceptions.Timeout:
            self.failure.emit("Сервер не отвечает")
        except Exception as e:
            self.failure.emit(str(e))
