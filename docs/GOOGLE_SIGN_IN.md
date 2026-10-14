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
6. Open your invite link, select Connect Google in the portal, and complete Google
   sign-in. Later visits can use Sign in with Google. The invite link also works.

Google sign-in does not grant beta access on its own. Accounts are linked by the
verified stable Google subject, never by matching email addresses. Uninvited users
are asked to use their invite link first. Native service SSO uses the same portal
session regardless of whether it came from Google or an invite.

References: [Google setup](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid)
and [OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect).
