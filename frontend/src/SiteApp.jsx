import { lazy, Suspense, useEffect, useState } from 'react'
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
} from 'react-router-dom'
import SiteLayout from './publicSite/components/SiteLayout.jsx'
import AboutPage from './publicSite/pages/AboutPage.jsx'
import AffiliateDisclosurePage from './publicSite/pages/AffiliateDisclosurePage.jsx'
import ContactPage from './publicSite/pages/ContactPage.jsx'
import HomePage from './publicSite/pages/HomePage.jsx'
import NotFoundPage from './publicSite/pages/NotFoundPage.jsx'
import PrivacyPolicyPage from './publicSite/pages/PrivacyPolicyPage.jsx'
import TermsPage from './publicSite/pages/TermsPage.jsx'
import './publicSite/styles.css'

const DashboardApp = lazy(() => import('./App.jsx'))

function DashboardAccess() {
  const [status, setStatus] = useState('checking')
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false

    async function checkAuthentication() {
      try {
        const response = await fetch('/auth/me', {
          credentials: 'same-origin',
        })
        if (!response.ok) {
          throw new Error(
            `Authentication check failed (HTTP ${response.status}).`,
          )
        }

        const data = await response.json()
        if (typeof data?.authenticated !== 'boolean') {
          throw new Error('Authentication service returned an invalid response.')
        }

        if (!cancelled) {
          setStatus(data.authenticated ? 'authenticated' : 'unauthenticated')
        }
      } catch (caughtError) {
        if (!cancelled) {
          setError(
            caughtError instanceof Error
              ? caughtError.message
              : 'Could not verify your sign-in.',
          )
          setStatus('error')
        }
      }
    }

    checkAuthentication()
    return () => {
      cancelled = true
    }
  }, [])

  if (status === 'checking') {
    return (
      <main className="route-access-status" role="status">
        Checking your sign-in...
      </main>
    )
  }

  if (status === 'unauthenticated') {
    return <Navigate to="/" replace />
  }

  if (status === 'error') {
    return (
      <main className="route-access-status" role="alert">
        <p>Could not verify your sign-in: {error}</p>
        <Link to="/">Return to the public website</Link>
      </main>
    )
  }

  return (
    <Suspense
      fallback={
        <main className="route-access-status" role="status">
          Loading your dashboard...
        </main>
      }
    >
      <DashboardApp />
    </Suspense>
  )
}

function PublicPages() {
  return (
    <Routes>
      <Route element={<SiteLayout />}>
        <Route index element={<HomePage />} />
        <Route path="about" element={<AboutPage />} />
        <Route path="contact" element={<ContactPage />} />
        <Route path="privacy" element={<PrivacyPolicyPage />} />
        <Route path="terms" element={<TermsPage />} />
        <Route
          path="affiliate-disclosure"
          element={<AffiliateDisclosurePage />}
        />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}

export default function SiteApp() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/dashboard/*" element={<DashboardAccess />} />
        <Route path="/*" element={<PublicPages />} />
      </Routes>
    </BrowserRouter>
  )
}
