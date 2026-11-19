import { Link } from 'react-router-dom'
import PageMeta from '../components/PageMeta.jsx'

export default function NotFoundPage() {
  return (
    <section className="not-found page-container">
      <PageMeta
        title="Page not found — DealSync"
        description="The requested DealSync public website page could not be found."
      />
      <span className="not-found-code">404</span>
      <h1>This page isn’t in sync.</h1>
      <p>The address may be outdated, or the page may have moved.</p>
      <Link className="button button-primary" to="/">
        Back to home <span aria-hidden="true">→</span>
      </Link>
    </section>
  )
}
