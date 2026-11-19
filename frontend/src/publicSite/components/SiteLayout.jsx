import { NavLink, Outlet } from 'react-router-dom'
import { useState } from 'react'

const primaryLinks = [
  { to: '/', label: 'Home', end: true },
  { to: '/about', label: 'About' },
  { to: '/contact', label: 'Contact' },
]

const policyLinks = [
  { to: '/privacy', label: 'Privacy' },
  { to: '/terms', label: 'Terms' },
  { to: '/affiliate-disclosure', label: 'Affiliate disclosure' },
]

function Brand({ footer = false }) {
  return (
    <NavLink
      className={`brand${footer ? ' brand--footer' : ''}`}
      to="/"
      aria-label="DealSync home"
    >
      <span className="brand-mark" aria-hidden="true">
        D
      </span>
      <span className="brand-copy">
        <span className="brand-name">DealSync</span>
        <span className="brand-caption">THOUGHTFUL SHOPPING</span>
      </span>
    </NavLink>
  )
}

function SiteHeader() {
  const [menuOpen, setMenuOpen] = useState(false)
  const closeMenu = () => setMenuOpen(false)

  return (
    <header className="site-header">
      <div className="header-inner">
        <Brand />
        <button
          className="menu-toggle"
          type="button"
          aria-expanded={menuOpen}
          aria-controls="primary-navigation"
          onClick={() => setMenuOpen((open) => !open)}
        >
          <span className="menu-toggle-icon" aria-hidden="true">
            {menuOpen ? '×' : '☰'}
          </span>
          <span className="sr-only">{menuOpen ? 'Close menu' : 'Open menu'}</span>
        </button>
        <nav
          className={`primary-navigation${menuOpen ? ' is-open' : ''}`}
          id="primary-navigation"
          aria-label="Main navigation"
        >
          {primaryLinks.map((link) => (
            <NavLink
              key={link.to}
              to={link.to}
              end={link.end}
              onClick={closeMenu}
            >
              {link.label}
            </NavLink>
          ))}
          <NavLink to="/about" onClick={closeMenu}>
            Our purpose <span aria-hidden="true">↗</span>
          </NavLink>
          <a
            className="nav-cta"
            href="/auth/google/login"
            onClick={closeMenu}
          >
            Sign in
          </a>
        </nav>
      </div>
    </header>
  )
}

function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="footer-main">
        <div className="footer-about">
          <Brand footer />
          <p>
            A team-operated project exploring a more considered way to shop
            with AI.
          </p>
        </div>
        <div className="footer-links">
          <span className="footer-heading">Explore</span>
          <NavLink to="/about">About DealSync</NavLink>
          <NavLink to="/contact">Contact</NavLink>
        </div>
        <div className="footer-links">
          <span className="footer-heading">Policies</span>
          {policyLinks.map((link) => (
            <NavLink key={link.to} to={link.to}>
              {link.label}
            </NavLink>
          ))}
        </div>
      </div>
      <div className="footer-bottom">
        <span>© {new Date().getFullYear()} DealSync</span>
        <span>Thoughtful shopping, in sync.</span>
      </div>
    </footer>
  )
}

export default function SiteLayout() {
  return (
    <div className="public-site">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <SiteHeader />
      <main id="main-content" tabIndex="-1">
        <Outlet />
      </main>
      <SiteFooter />
    </div>
  )
}
