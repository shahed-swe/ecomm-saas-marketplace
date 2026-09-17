// Tenant-owned analytics tags. The page gets public ids only — API secrets and CAPI tokens stay
// on the server, and the purchase event is also posted server-side so ad blockers cannot hide it.
type Ids = { ga4?: string; meta?: string };

export function AnalyticsTags({ ids }: { ids: Ids }) {
  if (!ids.ga4 && !ids.meta) return null;
  const ga4 = ids.ga4?.replace(/[^A-Za-z0-9-]/g, "");
  const meta = ids.meta?.replace(/[^0-9]/g, "");
  return (
    <>
      {ga4 && (
        <>
          <script async src={`https://www.googletagmanager.com/gtag/js?id=${ga4}`} />
          <script
            dangerouslySetInnerHTML={{
              __html: `window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','${ga4}');`,
            }}
          />
        </>
      )}
      {meta && (
        <script
          dangerouslySetInnerHTML={{
            __html: `!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod?n.callMethod.apply(n,arguments):n.queue.push(arguments)};if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');fbq('init','${meta}');fbq('track','PageView');`,
          }}
        />
      )}
    </>
  );
}
