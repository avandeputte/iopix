// www.iopix.net answers with a permanent redirect to the same path on iopix.net, so the site has one address.
export async function onRequest({ request, next }) {
  const url = new URL(request.url);
  if (url.hostname === "www.iopix.net") {
    url.hostname = "iopix.net";
    return Response.redirect(url.toString(), 301);
  }
  return next();
}
