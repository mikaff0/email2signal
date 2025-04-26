import asyncio
import re
import requests
import json
import os
import sys
import email
import html2text

from typing import Dict
from urllib.parse import urljoin

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import Envelope, Session, SMTP
from sendmail import send_mail
from email import message_from_bytes
from email.policy import default

def header_decode(header):
    hdr = ""
    for text, encoding in email.header.decode_header(header):
        if isinstance(text, bytes):
            text = text.decode(encoding or "us-ascii")
        hdr += text
    return hdr

class EmailHandler:
    def __init__(self, config: Dict[str, str]):
        self.receiver_regex = re.compile(r"(\+?\d+)@signal.localdomain")
        self.subject_regex = re.compile(r"Subject: (.*)\n")
        self.image_regex = re.compile(
            r'Content-Type: image/png; name=".*"\n+((?:[A-Za-z\d+/]{4}|\n)*(?:[A-Za-z\d+/]{2}==|[A-Za-z\d+/]{3}=)?)'
        )
        self.config = config

    async def handle_RCPT(
        self, server: SMTP, session: Session, envelope: Envelope, address, rcpt_options: list[str]
    ) -> str:
        # match and process signal number
        if match := re.search(self.receiver_regex, address):
            try:
                number = match.group(1)
            except TypeError:
                return "500 Malformed receiver address"

            if not address.startswith("+"):
                number = "+" + number

            envelope.rcpt_tos.append(number)
        # simply append normal mail address
        else:
            envelope.rcpt_tos.append(address)

        return "250 OK"

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        signal_numbers = []
        mail_addresses = []
        for addr in envelope.rcpt_tos:
            # a real email address cannot start with a special char
            if addr.startswith("+"):
                signal_numbers.append(addr)
            else:
                mail_addresses.append(addr)

        # send signal message if required
        if len(signal_numbers) > 0:
            print("Forwarding message to signal")
            success = await self.send_signal(envelope, signal_numbers)

            if not success:
                return "554 Sending signal message has failed"

        # send email if required
        if len(mail_addresses) == 0:
            return "250 Message accepted for delivery"
        else:
            envelope.rcpt_tos = mail_addresses

            print(f"Sending email via MTA. From: {envelope.mail_from} To: {envelope.rcpt_tos}")
            return send_mail(
                self.config["smtp_host"],
                int(self.config["smtp_port"]),
                self.config["smtp_user"],
                self.config["smtp_passwd"],
                envelope,
            )

    async def send_signal(self, envelope: Envelope, signal_receivers: list[str]) -> bool:
        # Parse the email using the standard library
        mail = message_from_bytes(envelope.content, policy=default)
        body_part = mail.get_body(('html', 'plain'))
        body = body_part.get_content() if body_part else ""
        print("body", body)
        
        payload = {}
        
        # Decode subject line correctly (handles encoded headers)
        subject = str(header_decode(mail.get('Subject')))
        print("subject", subject)
        msg = subject + "\r\n"

        # Convert HTML to plain text if present
        if "<!DOCTYPE html " in body:
            html = "<!DOCTYPE html " + body.split('<!DOCTYPE html ', 1)[-1]
            msg += html2text.html2text(html)
        else:
            msg += body

        payload["message"] = msg
        payload["number"] = self.config["sender_number"].replace("\\", "")
        payload["recipients"] = signal_receivers

        # --- Image processing from Function 1 ---
        # Look for embedded base64 images in the body (e.g., <img src="data:image/...">)
        image_regex = r"data:image\/[a-zA-Z]+;base64,([a-zA-Z0-9+/=\n\r]+)"
        matches = re.findall(image_regex, body)

        if matches:
            # Clean up line breaks in base64 data
            cleaned_images = [img.replace("\n", "").replace("\r", "") for img in matches]
            payload["base64_attachments"] = cleaned_images

        headers = {"Content-Type": "application/json"}

        url = urljoin(self.config["signal_rest_url"], "v2/send")
        response = requests.request("POST", url, headers=headers, data=json.dumps(payload))

        if response.status_code == 201:
            return True
        else:
            return False


async def amain(loop: asyncio.AbstractEventLoop):
    try:
        config = {
            "signal_rest_url": os.environ["SIGNAL_REST_URL"],
            "sender_number": os.environ["SENDER_NUMBER"],
            "smtp_host": os.environ["SMTP_HOST"],
            "smtp_user": os.environ["SMTP_USER"],
            "smtp_passwd": os.environ["SMTP_PASSWORD"],
            "smtp_port": os.getenv("SMTP_PORT", "587"),
        }
    except KeyError:
        sys.exit("Please set the required environment variables.")

    print("Starting email2signal server")
    email_handler = EmailHandler(config)
    controller = Controller(email_handler, hostname="")
    controller.start()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(amain(loop=loop))
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
