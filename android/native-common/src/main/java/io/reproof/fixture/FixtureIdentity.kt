package io.reproof.fixture

import android.content.Context
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.os.Build
import java.io.ByteArrayInputStream
import java.security.MessageDigest
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate

/** Public certificate pins identify the operator's existing helper; no credential is generated. */
object FixtureIdentity {
    const val HELPER_PACKAGE = "io.reproof.live"
    // Public AOSP testkey certificate, blob e242d83e2bf72169ab0abd5280d4d455caef71eb.
    // https://android.googlesource.com/platform/build/+/refs/heads/main/target/product/security/testkey.x509.pem
    private val publicTestCertificates = setOf("a40da80a59d170caa950cf15c18c454d47a39b26989d8b640ecd745ba71bf5dc")
    fun permittedSignerDigest(value: String) = value.matches(FixtureChallenge.DIGEST) && value !in publicTestCertificates
    fun verifiedUid(context: Context, packageName: String, signerDigest: String? = null): Int? { return try {
        val pm = context.packageManager
        if (Build.VERSION.SDK_INT < 28) return null
        val info = pm.getPackageInfo(packageName, PackageManager.GET_SIGNING_CERTIFICATES)
        val app = info.applicationInfo ?: return null
        if ((app.flags and ApplicationInfo.FLAG_DEBUGGABLE) == 0 || app.processName != packageName ||
            pm.getPackagesForUid(app.uid)?.toSet() != setOf(packageName) ||
            info.sharedUserId != null || !app.splitSourceDirs.isNullOrEmpty()) return null
        if (signerDigest != null) {
            if (!permittedSignerDigest(signerDigest)) return null
            val signing = info.signingInfo ?: return null
            if (signing.hasMultipleSigners() || signing.hasPastSigningCertificates()) return null
            val signatures = signing.apkContentsSigners ?: return null
            if (signatures.size != 1) return null
            val bytes = signatures.single().toByteArray()
            val digest = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }
            if (digest != signerDigest) return null
            val certificate = CertificateFactory.getInstance("X.509").generateCertificate(ByteArrayInputStream(bytes)) as X509Certificate
            // Android's default debug identity is deliberately not a privileged principal.
            if (certificate.subjectX500Principal.name.split(',').any {
                    it.trim().equals("CN=Android Debug", ignoreCase = true) }) return null
        }
        app.uid
    } catch (_: Throwable) { null }
    }
}
