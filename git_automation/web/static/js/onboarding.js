// onboarding.js — first-run modal: git identity form + GitHub token field.
//
// Flow: open when `needs_onboarding` is true. Each submit posts to the backend,
// re-fetches identity, and closes the modal once `needs_onboarding` is false.

import * as api from './api.js';
import { openModal, setLoading, setFieldError, toast } from './ui.js';

/**
 * Wire up the onboarding modal.
 * @param {(identity:object)=>void} onComplete  called with the final identity
 *        once onboarding is finished (or skipped).
 * @returns {{ open: (identity:object)=>void }}
 */
export function initOnboarding(onComplete) {
  const overlay = document.getElementById('onboarding-overlay');
  const form = document.getElementById('onboarding-form');

  const nameInput = document.getElementById('ob-name');
  const emailInput = document.getElementById('ob-email');
  const tokenInput = document.getElementById('ob-token');

  const fieldEmail = document.getElementById('field-email');
  const fieldToken = document.getElementById('field-token');

  const btnSaveIdentity = document.getElementById('btn-save-identity');
  const btnSaveToken = document.getElementById('btn-save-token');
  const btnSkip = document.getElementById('btn-skip-onboarding');
  const btnDone = document.getElementById('btn-done-onboarding');

  const helpToggle = document.getElementById('token-help-toggle');
  const helpBox = document.getElementById('token-help');
  const helpSteps = document.getElementById('token-help-steps');

  let closeModal = null;
  let latestIdentity = null;
  let instructionsLoaded = false;

  function close() {
    if (closeModal) closeModal();
    closeModal = null;
    if (onComplete) onComplete(latestIdentity);
  }

  // Reflect the latest identity into the form + maybe auto-close.
  function applyIdentity(identity) {
    latestIdentity = identity;
    if (identity.git_name) nameInput.value = identity.git_name;
    if (identity.git_email) emailInput.value = identity.git_email;
    if (!identity.needs_onboarding) {
      close();
      return true;
    }
    return false;
  }

  async function loadInstructions() {
    if (instructionsLoaded) return;
    try {
      const data = await api.getLoginInstructions();
      const steps = (data.steps || [])
        .map((step, i) => `${i + 1}. ${step}`)
        .join('\n');
      helpSteps.textContent = steps || 'No instructions available.';
      instructionsLoaded = true;
    } catch (err) {
      helpSteps.textContent = `Could not load instructions: ${err.message}`;
    }
  }

  helpToggle.addEventListener('click', async (e) => {
    e.preventDefault();
    const expanded = helpToggle.getAttribute('aria-expanded') === 'true';
    if (!expanded) await loadInstructions();
    helpToggle.setAttribute('aria-expanded', String(!expanded));
    helpBox.hidden = expanded;
  });

  // Save git identity.
  async function saveIdentity() {
    const name = nameInput.value.trim();
    const email = emailInput.value.trim();
    if (!name || !email) {
      setFieldError(fieldEmail, true);
      emailInput.focus();
      return;
    }
    setFieldError(fieldEmail, false);
    setLoading(btnSaveIdentity, true);
    try {
      const identity = await api.setGitIdentity(name, email);
      toast('success', 'Git identity saved', `${name} <${email}>`);
      applyIdentity(identity);
    } catch (err) {
      toast('error', 'Could not save identity', err.message);
    } finally {
      setLoading(btnSaveIdentity, false);
    }
  }

  // Connect GitHub via token.
  async function saveToken() {
    const token = tokenInput.value.trim();
    if (!token) {
      setFieldError(fieldToken, true);
      tokenInput.focus();
      return;
    }
    setFieldError(fieldToken, false);
    setLoading(btnSaveToken, true);
    try {
      const identity = await api.loginWithToken(token);
      tokenInput.value = '';
      toast('success', 'GitHub connected', identity.gh_user ? `Signed in as ${identity.gh_user}` : 'Authenticated.');
      applyIdentity(identity);
    } catch (err) {
      setFieldError(fieldToken, true);
      toast('error', 'GitHub authentication failed', err.message);
    } finally {
      setLoading(btnSaveToken, false);
    }
  }

  btnSaveIdentity.addEventListener('click', saveIdentity);
  btnSaveToken.addEventListener('click', saveToken);

  // Submitting the form (Enter) saves whichever section the user is in.
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (document.activeElement === tokenInput) saveToken();
    else saveIdentity();
  });

  btnSkip.addEventListener('click', close);
  btnDone.addEventListener('click', close);

  return {
    open(identity) {
      latestIdentity = identity;
      applyIdentityFields(identity);
      closeModal = openModal(overlay, { onClose: () => {} });
    },
  };

  function applyIdentityFields(identity) {
    if (identity.git_name) nameInput.value = identity.git_name;
    if (identity.git_email) emailInput.value = identity.git_email;
  }
}
