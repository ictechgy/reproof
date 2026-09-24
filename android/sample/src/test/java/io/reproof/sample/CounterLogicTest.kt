package io.reproof.sample

import org.junit.Assert.assertEquals
import org.junit.Test

class CounterLogicTest {
    @Test
    fun addingOneItemIncrementsCountByOne() {
        assertEquals(1, CounterLogic.increment())
    }
}
