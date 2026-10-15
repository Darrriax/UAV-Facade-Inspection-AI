<script setup lang="ts">
/**
 * Root component: the active route, plus the toast layer.
 *
 * Only two things live here on purpose. The shell (header, navigation, account
 * block) belongs to the *layout* routes - `InspectorLayout`, and later the Admin
 * one - so it is not repeated for the auth screens, which have no chrome at all.
 * The toast host is the exception: it has to survive every navigation to be able
 * to report one.
 */

import { RouterView } from 'vue-router'

import ToastHost from '@/components/ToastHost.vue'
</script>

<template>
  <RouterView />
  <ToastHost />
</template>
