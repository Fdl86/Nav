import kotlin.math.*

class NavEngine {
    companion object {
        private const val EARTH_RADIUS_NM = 3440.065 // Rayon Terre en NM
        private const val MIN_GROUND_SPEED = 20.0 // Vitesse sol minimale (noeuds)
    }

    data class WindResult(
        val groundSpeed: Int,
        val windCorrectionAngle: Int
    )

    /**
     * Calcule le prochain point de navigation (Loxodromie)
     * @param lat1 Latitude de départ en degrés décimaux
     * @param lon1 Longitude de départ en degrés décimaux
     * @param track Cap vrai en degrés
     * @param distance Distance à parcourir en NM
     * @return Pair(latitude, longitude) du point suivant en degrés décimaux
     */
    fun calculateNextWaypoint(
        lat1: Double, 
        lon1: Double, 
        track: Double, 
        distance: Double
    ): Pair<Double, Double> {
        require(track in 0.0..360.0) { "Le cap doit être entre 0 et 360 degrés" }
        require(distance >= 0) { "La distance ne peut pas être négative" }

        val bearing = Math.toRadians(track)
        val lat1Rad = Math.toRadians(lat1)
        
        // Calcul de la nouvelle latitude
        val lat2Rad = lat1Rad + (distance / EARTH_RADIUS_NM) * cos(bearing)
        val lat2 = Math.toDegrees(lat2Rad)
        
        // Calcul de la nouvelle longitude (protection contre la division par zéro)
        val lon2 = if (abs(cos(lat1Rad)) > 1e-10) {
            val lon1Rad = Math.toRadians(lon1)
            val deltaLon = (distance / EARTH_RADIUS_NM) * sin(bearing) / cos(lat1Rad)
            Math.toDegrees(lon1Rad + deltaLon)
        } else {
            lon1 // Évite la division par zéro aux pôles
        }
        
        return Pair(lat2, lon2)
    }

    /**
     * Calcule le vent effectif, la vitesse sol et l'angle de dérive
     * @param tas True Air Speed en noeuds
     * @param track Cap vrai en degrés
     * @param windDirection Direction du vent en degrés (d'où vient le vent)
     * @param windSpeed Vitesse du vent en noeuds
     * @return WindResult contenant vitesse sol et angle de dérive
     */
    fun calculateWindEffects(
        tas: Double, 
        track: Double, 
        windDirection: Double, 
        windSpeed: Double
    ): WindResult {
        require(tas > 0) { "La TAS doit être positive" }
        require(windSpeed >= 0) { "La vitesse du vent ne peut pas être négative" }

        // Angle entre la route et le vent
        val windAngle = Math.toRadians(windDirection - track)
        
        // Calcul de l'angle de dérive (Wind Correction Angle)
        val wca = calculateWindCorrectionAngle(tas, windSpeed, windAngle)
        
        // Calcul de la vitesse sol (Ground Speed)
        val gs = calculateGroundSpeed(tas, windSpeed, windAngle, wca)
        
        return WindResult(
            groundSpeed = max(MIN_GROUND_SPEED, gs).roundToInt(),
            windCorrectionAngle = wca.roundToInt()
        )
    }

    private fun calculateWindCorrectionAngle(
        tas: Double, 
        windSpeed: Double, 
        windAngle: Double
    ): Double {
        val sinWca = (windSpeed / tas) * sin(windAngle)
        return when {
            abs(sinWca) <= 1 -> Math.toDegrees(asin(sinWca))
            sinWca > 1 -> 90.0 // Cas limite
            else -> -90.0 // Cas limite
        }
    }

    private fun calculateGroundSpeed(
        tas: Double, 
        windSpeed: Double, 
        windAngle: Double, 
        wca: Double
    ): Double {
        val wcaRad = Math.toRadians(wca)
        return (tas * cos(wcaRad)) - (windSpeed * cos(windAngle))
    }

    // Fonctions utilitaires pour les conversions
    fun degreesToRadians(degrees: Double): Double = Math.toRadians(degrees)
    fun radiansToDegrees(radians: Double): Double = Math.toDegrees(radians)
}
