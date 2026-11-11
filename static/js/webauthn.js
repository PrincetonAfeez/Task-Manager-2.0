/* WebAuthn ceremony helpers (registration and authentication). */
(function () {
  "use strict";

  function getCsrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function csrfHeaders(extra) {
    var headers = { "X-CSRFToken": getCsrfToken(), Accept: "application/json" };
    if (extra) {
      Object.keys(extra).forEach(function (key) {
        headers[key] = extra[key];
      });
    }
    return headers;
  }

  function bufferToBase64URL(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    bytes.forEach(function (b) {
      binary += String.fromCharCode(b);
    });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function base64URLToBuffer(value) {
    var base64 = value.replace(/-/g, "+").replace(/_/g, "/");
    var pad = base64.length % 4;
    if (pad) base64 += "=".repeat(4 - pad);
    var binary = atob(base64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  function publicKeyCredentialToJSON(credential) {
    if (!credential) return null;
    var response = credential.response;
    var json = {
      id: credential.id,
      rawId: bufferToBase64URL(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: bufferToBase64URL(response.clientDataJSON),
        authenticatorData: bufferToBase64URL(response.authenticatorData),
        signature: bufferToBase64URL(response.signature),
        userHandle: response.userHandle ? bufferToBase64URL(response.userHandle) : null,
      },
      clientExtensionResults: credential.getClientExtensionResults(),
    };
    if (response.attestationObject) {
      json.response.attestationObject = bufferToBase64URL(response.attestationObject);
    }
    if (response.getTransports) {
      json.transports = response.getTransports();
    }
    return json;
  }

  function prepareCreationOptions(options) {
    var copy = JSON.parse(JSON.stringify(options));
    copy.challenge = base64URLToBuffer(copy.challenge);
    copy.user.id = base64URLToBuffer(copy.user.id);
    if (copy.excludeCredentials) {
      copy.excludeCredentials = copy.excludeCredentials.map(function (desc) {
        return Object.assign({}, desc, { id: base64URLToBuffer(desc.id) });
      });
    }
    return copy;
  }

  function prepareRequestOptions(options) {
    var copy = JSON.parse(JSON.stringify(options));
    copy.challenge = base64URLToBuffer(copy.challenge);
    if (copy.allowCredentials) {
      copy.allowCredentials = copy.allowCredentials.map(function (desc) {
        return Object.assign({}, desc, { id: base64URLToBuffer(desc.id) });
      });
    }
    return copy;
  }

  async function runCeremony(beginUrl, finishUrl, finishPayload) {
    var beginRes = await fetch(beginUrl, {
      method: "POST",
      headers: csrfHeaders(),
      credentials: "same-origin",
    });
    var beginData = await beginRes.json();
    if (!beginRes.ok) throw new Error(beginData.error || "Could not start passkey ceremony.");

    var isCreate = Boolean(beginData.options.user);
    var publicKey = isCreate
      ? prepareCreationOptions(beginData.options)
      : prepareRequestOptions(beginData.options);
    var credential = await navigator.credentials[isCreate ? "create" : "get"]({ publicKey: publicKey });
    var body = Object.assign({}, finishPayload || {}, { credential: publicKeyCredentialToJSON(credential) });

    var finishRes = await fetch(finishUrl, {
      method: "POST",
      headers: csrfHeaders({ "Content-Type": "application/json" }),
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
    var finishData = await finishRes.json();
    if (!finishRes.ok) throw new Error(finishData.error || "Passkey verification failed.");
    return finishData;
  }

  window.TaskManagerWebAuthn = {
    runCeremony: runCeremony,
  };
})();
