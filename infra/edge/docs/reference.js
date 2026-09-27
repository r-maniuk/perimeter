// The API reference, drawn from the live OpenAPI document. Everything on this page is served by
// the edge itself, so its policy admits scripts from this origin only. "Try it out" requests carry
// the dashboard's session cookie (same origin); a device token or a token from POST /v1/token goes
// in through "Authorize".
window.ui = SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  layout: "BaseLayout",
  deepLinking: true,
  persistAuthorization: true,
  displayRequestDuration: true,
  tryItOutEnabled: false,
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
});
