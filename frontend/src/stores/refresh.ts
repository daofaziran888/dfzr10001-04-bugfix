import { defineStore } from 'pinia'

/** 跨页面刷新信号：增量游标提交水位后，采集任务页与工作台跟着刷新。 */
export const useRefreshStore = defineStore('refresh', {
  state: () => ({
    dataVersion: 0,
  }),
  actions: {
    bump() {
      this.dataVersion += 1
    },
  },
})
