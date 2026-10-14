# Google sign-in for swarm.forum

1. Create/select a Google Cloud project and open Google Auth Platform.
2. Under Branding, set the app name to Swarm Forum and configure support/contact email.
3. Select External audience, keep Testing during the friends beta, and add test users.
4. Under Clients, create a Web application client. Set its authorized redirect URI to
   `https://swarm.forum/api/google-callback`. JavaScript origins are not needed for
   this server-side flow. Only openid, email and profile scopes are requested.
5. Store HOSTING_GOOGLE_CLIENT_ID and HOSTING_GOOGLE_CLIENT_SECRET in the private
   operator environment, then deploy them to the hosting control-plane environment.
   Never use public Vercel environment variables or commit these values.
6. Select Sign in with Google. New accounts see an invite-code form after sign-in.
   Enter a private invite code to unlock the swarm. Private invite links carry the
   code through sign-in and redeem it automatically. Returning users sign in directly.

Google sign-in alone does not grant access to provisioning or provider settings.
Accounts are linked by verified stable Google subject, never by matching emails.
Existing invited workspaces and their data are preserved when claiming an invite.
Native service SSO uses the same portal session.

References: [Google setup](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid)
and [OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect).
