pkgname=umbriel-session
pkgver=0.1.0
pkgrel=1
pkgdesc="Session save and restore for the Umbriel Wayland compositor"
arch=('any')
url="https://github.com/noctalia-dev/umbriel-session"
license=('MIT')
depends=('python' 'python-rich')
makedepends=('python-setuptools' 'python-wheel')
source=("$pkgname-$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
    cd "$pkgname-$pkgver"
    python -m build --wheel --no-isolation 2>/dev/null || true
}

package() {
    cd "$pkgname-$pkgver"
    # Direct install (no build step required)
    local site="$pkgdir/usr/lib/python$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')/site-packages"
    mkdir -p "$site" "$pkgdir/usr/bin"
    cp -r src/umbriel_session "$site/"
    cat > "$pkgdir/usr/bin/umbriel-session" <<EOF
#!/bin/sh
exec /usr/bin/python3 -m umbriel_session.cli "\$@"
EOF
    chmod 755 "$pkgdir/usr/bin/umbriel-session"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}